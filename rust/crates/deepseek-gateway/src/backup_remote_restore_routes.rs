//! Native S5 remote target restore orchestration under `restore_staging_store`:
//! - `POST /api/workspace/restores/from-target`
//! - `POST /api/workspace/restores/from-target/preview`
//! - `POST /api/workspace/restores/{restore_id}/fetch`
//! - `POST /api/workspace/restores/{restore_id}/preflight`
//! - `POST /api/workspace/restores/{restore_id}/materialize`
//!
//! Enforces `restore_staging_store` domain authority: executes only under
//! `DEEPSEEK_RUNTIME_MODE=python_disabled` and refuses with
//! `409 NATIVE_RESTORE_STAGING_STORE_WRITE_NOT_OWNED` without creating directories otherwise.

use std::collections::{BTreeSet, HashMap, HashSet};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::time::Instant;

use axum::body::Bytes;
use axum::extract::Path as AxumPath;
use axum::http::{HeaderMap, Method, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use axum::{Json, Router};
use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::backup_mirror::sha256_file;
use deepseek_policy::backup_projection::{
    ChainPackage, OBJECT_SET_V1, ProjectionPlan, RestoreSelection, estimate_safety_backup_peak,
    load_verified_plan, manifest_files, materialize_chain, normalize_selection, plan_projection,
    resolve_lineage_from_receipts, sha256_bytes, validate_control_component_inventory,
    write_verified_plan,
};
use deepseek_policy::backup_unlock::{
    PACK_INDEX_PATH, SelectedArchiveFilter, decrypt_file_native, decrypt_passphrase_file_native,
    extract_archive_metadata, extract_selected_archive, is_plain_sha256, safe_extract_and_verify,
    scrub_plaintext_file, verify_control_metadata, verify_manifest_tree,
};
use deepseek_policy::core_utils::python_truthy;
use deepseek_policy::entropy::{Entropy, SystemEntropy};
use deepseek_policy::python_json::value_str;
use deepseek_storage::object_set::{ObjectInventoryEntry, object_inventory_digest};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use crate::backup_archive_routes::{
    InspectMeta, estimate_local_safety_logical_bytes, inspect_projected_restore_tree,
    inspect_verified_tree, tree_digest,
};
use crate::backup_capacity_routes::{filesystem_disk_usage, now_utc_iso};
use crate::backup_catalog_routes::{catalog_state, registered_target, utc_iso_from_unix};
use crate::backup_dr_audit_routes::{commit_marker_valid, resolve_target_root, sha256_hex};
use crate::backup_dr_slo_routes::now_unix_secs;
use crate::backup_restore_read_routes::{
    get_restore, read_json_object, validate_backup_id, validate_restore_id,
};
use crate::backup_restore_tx_routes::{
    advance_federated_phase, converge_job_control, prepare_restore, read_remote_restore_session,
    release_session_holds, remote_session_path, write_atomic_remote_session,
};
use crate::backup_restore_write_routes::{finish_json, require_write_authority, write_atomic_json};
use crate::backup_secret_slots::{
    consume_secret, has_secret, put_secret_bytes, record_unlock_failure,
};
use crate::route_support;

const MAX_BODY_BYTES: i64 = 64_000;
const SESSION_SCHEMA_VERSION: i64 = 3;
const HOLD_TTL_SECONDS: u64 = 6 * 3600;
const DEFAULT_DISK_RESERVE_BYTES: u64 = 1024 * 1024 * 1024;
const OBJECT_SET_NOT_FETCHED_ERR: &str = "Restore session has not finished fetching";
const RECOVERY_PREFLIGHT_CAPACITY_CODE: &str = "recovery_preflight_capacity";

pub fn router() -> Router {
    Router::new()
        .route(
            "/api/workspace/restores/from-target/preview",
            any(dispatch_from_target_preview),
        )
        .route(
            "/api/workspace/restores/from-target",
            any(dispatch_from_target),
        )
        .route(
            "/api/workspace/restores/:restore_id/fetch",
            any(dispatch_fetch),
        )
        .route(
            "/api/workspace/restores/:restore_id/preflight",
            any(dispatch_preflight),
        )
        .route(
            "/api/workspace/restores/:restore_id/materialize",
            any(dispatch_materialize),
        )
}

async fn dispatch_from_target(method: Method, headers: HeaderMap, body: Bytes) -> Response {
    if method != Method::POST {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    if let Err(err) = require_write_authority() {
        return route_support::app_error_response(err);
    }
    let payload = match route_support::read_json_object(&headers, &body, MAX_BODY_BYTES) {
        Ok(map) => map,
        Err(err) => return route_support::app_error_response(err),
    };
    let joined = tokio::task::spawn_blocking(move || {
        let root = crate::data_routes::workspace_root();
        let target_id = payload
            .get("targetId")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        let backup_id = payload
            .get("backupId")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        let complete = payload.get("complete").is_some_and(python_truthy);
        if complete {
            return restore_from_target(&root, &target_id, &backup_id);
        }
        let restore_id = payload
            .get("restoreId")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .filter(|s| !s.is_empty());
        create_restore_from_target(
            &root,
            &target_id,
            &backup_id,
            payload.get("selection"),
            restore_id.as_deref(),
        )
    })
    .await;
    finish_json(joined)
}

async fn dispatch_from_target_preview(method: Method, headers: HeaderMap, body: Bytes) -> Response {
    if method != Method::POST {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    if let Err(err) = require_write_authority() {
        return route_support::app_error_response(err);
    }
    let payload = match route_support::read_json_object(&headers, &body, MAX_BODY_BYTES) {
        Ok(map) => map,
        Err(err) => return route_support::app_error_response(err),
    };
    let joined = tokio::task::spawn_blocking(move || {
        let root = crate::data_routes::workspace_root();
        let target_id = payload
            .get("targetId")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        let backup_id = payload
            .get("backupId")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        let restore_id = payload
            .get("restoreId")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .filter(|s| !s.is_empty());
        preview_restore_from_target(
            &root,
            &target_id,
            &backup_id,
            payload.get("selection"),
            restore_id.as_deref(),
        )
    })
    .await;
    finish_json(joined)
}

async fn dispatch_fetch(
    method: Method,
    AxumPath(restore_id): AxumPath<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if method != Method::POST {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    if let Err(err) = require_write_authority() {
        return route_support::app_error_response(err);
    }
    let payload = match route_support::read_json_object(&headers, &body, MAX_BODY_BYTES) {
        Ok(map) => map,
        Err(err) => return route_support::app_error_response(err),
    };
    let max_bytes = match payload.get("maxBytes") {
        None | Some(Value::Null) => None,
        Some(val) => match val.as_u64() {
            Some(n) => Some(n),
            None => match val.as_i64() {
                Some(i) if i >= 0 => Some(i as u64),
                _ => {
                    return route_support::app_error_response(AppError::invalid_payload(
                        "maxBytes must be an integer",
                    ));
                }
            },
        },
    };
    let joined = tokio::task::spawn_blocking(move || {
        let root = crate::data_routes::workspace_root();
        fetch_restore_session(&root, &restore_id, max_bytes)
    })
    .await;
    finish_json(joined)
}

async fn dispatch_preflight(
    method: Method,
    AxumPath(restore_id): AxumPath<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if method != Method::POST {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    if let Err(err) = require_write_authority() {
        return route_support::app_error_response(err);
    }
    let payload = match route_support::read_json_object(&headers, &body, MAX_BODY_BYTES) {
        Ok(map) => map,
        Err(err) => return route_support::app_error_response(err),
    };
    if !payload.is_empty() {
        return route_support::app_error_response(AppError::invalid_payload(
            "Recovery preflight does not accept client capacity overrides",
        ));
    }
    let joined = tokio::task::spawn_blocking(move || {
        let root = crate::data_routes::workspace_root();
        preflight_restore_session(&root, &restore_id)
    })
    .await;
    match joined {
        Ok(Ok(report)) => Json(report).into_response(),
        Ok(Err(PreflightFailure::Error(err))) => route_support::app_error_response(err),
        Ok(Err(PreflightFailure::Blocked { code, report })) => (
            StatusCode::CONFLICT,
            Json(json!({
                "error": "Recovery preflight is blocked",
                "code": code,
                "details": {"preflight": report},
            })),
        )
            .into_response(),
        Err(_) => route_support::app_error_response(route_support::unhandled_error()),
    }
}

async fn dispatch_materialize(
    method: Method,
    AxumPath(restore_id): AxumPath<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if method != Method::POST {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    if let Err(err) = require_write_authority() {
        return route_support::app_error_response(err);
    }
    let payload = match route_support::read_json_object(&headers, &body, MAX_BODY_BYTES) {
        Ok(map) => map,
        Err(err) => return route_support::app_error_response(err),
    };
    let joined = tokio::task::spawn_blocking(move || {
        let root = crate::data_routes::workspace_root();
        materialize_federated_restore(&root, &restore_id, &payload)
    })
    .await;
    finish_json(joined)
}

pub(crate) enum PreflightFailure {
    Error(AppError),
    Blocked { code: &'static str, report: Value },
}

impl From<AppError> for PreflightFailure {
    fn from(value: AppError) -> Self {
        Self::Error(value)
    }
}

fn conflict_err(msg: impl Into<String>) -> AppError {
    AppError {
        message: msg.into(),
        code: codes::INVALID_REQUEST,
        status: 409,
    }
}

fn truthy_str(value: Option<&Value>) -> String {
    match value {
        Some(v) if python_truthy(v) => value_str(v),
        _ => String::new(),
    }
}

fn object_path(target_root: &Path, digest: &str) -> Result<PathBuf, AppError> {
    if !is_plain_sha256(digest) {
        return Err(conflict_err("Backup object digest is invalid"));
    }
    let prefix: String = digest.chars().take(2).collect();
    Ok(target_root
        .join("objects")
        .join("sha256")
        .join(prefix)
        .join(format!("{digest}.age")))
}

fn receipt_path(target_root: &Path, backup_id: &str) -> Result<PathBuf, AppError> {
    validate_backup_id(backup_id)?;
    Ok(target_root
        .join("receipts")
        .join(format!("{backup_id}.json")))
}

fn hold_path(target_root: &Path, hold_key: &str) -> PathBuf {
    let mut path = target_root.to_path_buf();
    for seg in hold_key.trim_start_matches('/').split('/') {
        if !seg.is_empty() && seg != "." && seg != ".." {
            path.push(seg);
        }
    }
    path
}

fn put_json_if_absent(target_root: &Path, rel_key: &str, payload: &Value) -> Result<(), AppError> {
    let dest = hold_path(target_root, rel_key);
    if let Some(parent) = dest.parent() {
        fs::create_dir_all(parent).map_err(|_| route_support::unhandled_error())?;
    }
    let formatted =
        serde_json::to_string_pretty(payload).map_err(|_| route_support::unhandled_error())?;
    match OpenOptions::new().write(true).create_new(true).open(&dest) {
        Ok(mut handle) => {
            let _ = handle.write_all(formatted.as_bytes());
            let _ = handle.write_all(b"\n");
            let _ = handle.flush();
            Ok(())
        }
        Err(err) if err.kind() == std::io::ErrorKind::AlreadyExists => Ok(()),
        Err(_) => Err(route_support::unhandled_error()),
    }
}

fn target_markers(target_root: &Path) -> Vec<Map<String, Value>> {
    let commits_dir = target_root.join("commits");
    if !commits_dir.is_dir() {
        return Vec::new();
    }
    let mut stack = vec![commits_dir];
    let mut files = Vec::new();
    while let Some(dir) = stack.pop() {
        let Ok(entries) = fs::read_dir(&dir) else {
            continue;
        };
        for entry in entries.flatten() {
            let p = entry.path();
            if p.is_dir() {
                stack.push(p);
            } else if p.is_file() && p.extension().and_then(|e| e.to_str()) == Some("json") {
                files.push(p);
            }
        }
    }
    files.sort();
    let mut out = Vec::new();
    for p in files {
        let Ok(raw) = fs::read_to_string(&p) else {
            continue;
        };
        let Ok(Value::Object(obj)) = serde_json::from_str::<Value>(&raw) else {
            continue;
        };
        out.push(obj);
    }
    out
}

pub(crate) fn component_cache_dir(root: &Path) -> PathBuf {
    root.join(".backup-component-cache")
}

pub(crate) fn component_cache_path(cache_root: &Path, digest: &str) -> PathBuf {
    let canonical = digest
        .strip_prefix("sha256:")
        .unwrap_or(digest)
        .to_ascii_lowercase();
    let prefix: String = canonical.chars().take(2).collect();
    cache_root
        .join("sha256")
        .join(prefix)
        .join(format!("{canonical}.age"))
}

fn component_cache_partial_path(cache_root: &Path, digest: &str) -> PathBuf {
    let final_path = component_cache_path(cache_root, digest);
    let canonical = digest
        .strip_prefix("sha256:")
        .unwrap_or(digest)
        .to_ascii_lowercase();
    final_path.with_file_name(format!(".{canonical}.partial"))
}

fn inspect_cached_component(cache_root: &Path, digest: &str, expected_bytes: u64) -> bool {
    let path = component_cache_path(cache_root, digest);
    path.is_file()
        && fs::metadata(&path).map(|m| m.len()).ok() == Some(expected_bytes)
        && sha256_file(&path).ok().as_deref() == Some(digest)
}

fn random_restore_id() -> String {
    let hex = SystemEntropy.new_id().unwrap_or_else(|_| {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|elapsed| elapsed.as_nanos())
            .unwrap_or(0);
        format!("{nanos:016x}")
    });
    let suffix = &hex[..12.min(hex.len())];
    format!("restore_{suffix}")
}

fn committed_object_inventory(record: &Map<String, Value>) -> Result<Vec<(String, u64)>, AppError> {
    if record.get("storageProtocol").and_then(Value::as_str) == Some(OBJECT_SET_V1) {
        let raw_objects = record
            .get("objects")
            .and_then(Value::as_array)
            .ok_or_else(|| AppError::invalid_payload("object-set receipt inventory is invalid"))?;
        let control_digest = record
            .get("controlObjectDigest")
            .and_then(Value::as_str)
            .unwrap_or("");
        let expected_set_digest = record
            .get("objectSetDigest")
            .and_then(Value::as_str)
            .unwrap_or("");
        let mut entries = Vec::with_capacity(raw_objects.len());
        let mut parsed = Vec::with_capacity(raw_objects.len());
        let mut seen = HashSet::new();
        for item in raw_objects {
            let digest = item.get("digest").and_then(Value::as_str).ok_or_else(|| {
                AppError::invalid_payload("object-set receipt inventory is invalid")
            })?;
            let size = item.get("size").and_then(Value::as_u64).ok_or_else(|| {
                AppError::invalid_payload("object-set receipt inventory is invalid")
            })?;
            if !is_plain_sha256(digest) || !seen.insert(digest.to_string()) {
                return Err(AppError::invalid_payload(
                    "object-set receipt inventory is invalid",
                ));
            }
            entries.push(ObjectInventoryEntry {
                digest: digest.to_string(),
                size,
            });
            parsed.push((digest.to_string(), size));
        }
        let computed = object_inventory_digest(&entries)
            .map_err(|_| AppError::invalid_payload("object-set receipt inventory is invalid"))?;
        if !seen.contains(control_digest) || computed != expected_set_digest {
            return Err(AppError::invalid_payload(
                "object-set receipt digest mismatch",
            ));
        }
        return Ok(parsed);
    }
    let digest = record
        .get("objectDigest")
        .or_else(|| record.get("ciphertextSha256"))
        .and_then(Value::as_str)
        .unwrap_or("");
    if !is_plain_sha256(digest) {
        return Err(AppError::invalid_payload(
            "Backup receipt is missing object digest",
        ));
    }
    let size = record.get("size").and_then(Value::as_u64).unwrap_or(0);
    Ok(vec![(digest.to_string(), size)])
}

pub(crate) fn restore_members(session: &Map<String, Value>) -> Result<Vec<Value>, AppError> {
    let storage_proto = session
        .get("storageProtocol")
        .and_then(Value::as_str)
        .unwrap_or("");
    let snapshot_kind = session
        .get("snapshotKind")
        .and_then(Value::as_str)
        .unwrap_or("full");
    if storage_proto == OBJECT_SET_V1 || snapshot_kind == "incremental" {
        let Some(raw_chain) = session.get("chain").and_then(Value::as_array) else {
            return Err(conflict_err("Restore session has no valid chain"));
        };
        if raw_chain.is_empty() || raw_chain.iter().any(|v| !v.is_object()) {
            return Err(conflict_err("Restore session has no valid chain"));
        }
        return Ok(raw_chain.clone());
    }
    Ok(vec![json!({
        "backupId": truthy_str(session.get("backupId")),
        "objectDigest": truthy_str(session.get("objectDigest")),
        "filename": truthy_str(session.get("filename")),
        "expectedBytes": session.get("expectedBytes").and_then(Value::as_u64).unwrap_or(0),
        "downloadedBytes": session.get("downloadedBytes").and_then(Value::as_u64).unwrap_or(0),
        "ciphertextPath": truthy_str(session.get("ciphertextPath")),
        "snapshotKind": "full",
    })])
}

fn whole_age_chain_member(
    target_root: &Path,
    receipt: &Value,
    staging_root: &Path,
) -> Result<Value, AppError> {
    let b_id = truthy_str(receipt.get("backupId"));
    let digest = truthy_str(
        receipt
            .get("objectDigest")
            .or_else(|| receipt.get("ciphertextSha256")),
    );
    if !is_plain_sha256(&digest) {
        return Err(conflict_err(format!(
            "Backup receipt {b_id} is missing object digest"
        )));
    }
    let obj_file = object_path(target_root, &digest)?;
    let meta = fs::metadata(&obj_file).map_err(|_| {
        AppError::not_found(format!(
            "Backup ciphertext object is missing on target: {b_id}"
        ))
    })?;
    let filename = truthy_str(receipt.get("filename"));
    let chosen_name = if filename.is_empty() {
        format!("{b_id}.age")
    } else {
        filename
    };
    Ok(json!({
        "backupId": b_id,
        "snapshotKind": truthy_str(receipt.get("snapshotKind")),
        "parentBackupId": receipt.get("parentBackupId").cloned().unwrap_or(Value::Null),
        "baseBackupId": receipt.get("baseBackupId").cloned().unwrap_or(Value::Null),
        "objectDigest": digest,
        "filename": chosen_name.clone(),
        "expectedBytes": meta.len(),
        "downloadedBytes": 0,
        "remoteETag": Value::Null,
        "remoteVersionId": Value::Null,
        "ciphertextPath": staging_root.join(chosen_name).to_string_lossy().into_owned(),
    }))
}

fn object_set_member_descriptor(
    target_root: &Path,
    receipt: &Map<String, Value>,
    staging_root: &Path,
    index: usize,
) -> Result<Value, AppError> {
    let b_id = truthy_str(receipt.get("backupId"));
    let control_digest = truthy_str(receipt.get("controlObjectDigest"));
    let expected_set_digest = truthy_str(receipt.get("objectSetDigest"));
    let inventory = committed_object_inventory(receipt).map_err(|e| conflict_err(e.message))?;
    let control_size = inventory
        .iter()
        .find(|(d, _)| d == &control_digest)
        .map(|(_, s)| *s)
        .ok_or_else(|| conflict_err(format!("Backup receipt {b_id} control object is foreign")))?;
    let ctrl_file = object_path(target_root, &control_digest)?;
    let Ok(ctrl_meta) = fs::metadata(&ctrl_file) else {
        return Err(AppError::not_found(format!(
            "Committed object-set control is missing: {b_id}"
        )));
    };
    if ctrl_meta.len() != control_size {
        return Err(AppError::not_found(format!(
            "Committed object-set control is missing: {b_id}"
        )));
    }
    let remote_objects: Vec<Value> = inventory
        .iter()
        .map(|(d, s)| json!({"digest": d, "size": s}))
        .collect();
    let control = json!({
        "backupId": b_id,
        "objectDigest": control_digest,
        "expectedBytes": ctrl_meta.len(),
        "downloadedBytes": 0,
        "remoteETag": Value::Null,
        "remoteVersionId": Value::Null,
        "ciphertextPath": staging_root.join(format!("control-{index:04}.age")).to_string_lossy().into_owned(),
        "fetched": false,
    });
    let skind = {
        let s = truthy_str(receipt.get("snapshotKind"));
        if s.is_empty() { "full".to_string() } else { s }
    };
    Ok(json!({
        "backupId": b_id,
        "storageProtocol": OBJECT_SET_V1,
        "objectSetDigest": expected_set_digest,
        "controlObjectDigest": control_digest,
        "objects": remote_objects,
        "control": control,
        "snapshotKind": skind,
        "parentBackupId": receipt.get("parentBackupId").cloned().unwrap_or(Value::Null),
        "baseBackupId": receipt.get("baseBackupId").cloned().unwrap_or(Value::Null),
    }))
}

pub(crate) fn create_restore_from_target(
    root: &Path,
    target_id: &str,
    backup_id: &str,
    selection_val: Option<&Value>,
    restore_id_opt: Option<&str>,
) -> Result<Value, AppError> {
    let selection = normalize_selection(selection_val)?;
    let selection_digest = selection.as_ref().map(RestoreSelection::digest);

    if let Some(restore_id) = restore_id_opt.filter(|s| !s.is_empty()) {
        validate_restore_id(restore_id)?;
        let Some(mut existing) = read_remote_restore_session(root, restore_id)? else {
            return Err(AppError::not_found("Remote restore session not found"));
        };
        if truthy_str(existing.get("targetId")) != target_id
            || truthy_str(existing.get("backupId")) != backup_id
        {
            return Err(conflict_err(
                "Restore session source does not match the requested target and backup",
            ));
        }
        let mut frozen = existing
            .get("selectionDigest")
            .filter(|v| python_truthy(v))
            .map(value_str);
        if let Some(ref sd) = selection_digest {
            if frozen.as_deref().is_some_and(|ex_sd| ex_sd != sd) {
                return Err(conflict_err(
                    "Restore selection does not match the frozen session selection",
                ));
            }
            if frozen.is_none() {
                if let Some(ref sel) = selection {
                    existing.insert("selection".into(), sel.canonical_value());
                    existing.insert("selectionDigest".into(), Value::String(sd.clone()));
                    existing.insert("schemaVersion".into(), json!(SESSION_SCHEMA_VERSION));
                    existing.insert("updatedAt".into(), Value::String(now_utc_iso()));
                    let sp = remote_session_path(root, restore_id)?;
                    write_atomic_remote_session(&sp, &mut existing, false, false, false)?;
                    frozen = Some(sd.clone());
                }
            }
        }
        let holds: Vec<Value> = existing
            .get("holdKeys")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_else(|| {
                let hk = truthy_str(existing.get("holdKey"));
                if hk.is_empty() {
                    Vec::new()
                } else {
                    vec![Value::String(hk)]
                }
            });
        return Ok(json!({
            "restoreId": restore_id,
            "phase": existing.get("phase").and_then(Value::as_str).unwrap_or("fetching"),
            "targetId": truthy_str(existing.get("targetId")),
            "backupId": truthy_str(existing.get("backupId")),
            "selection": selection.as_ref().map_or_else(|| existing.get("selection").cloned().unwrap_or(Value::Null), RestoreSelection::canonical_value),
            "selectionDigest": frozen,
            "holds": holds,
        }));
    }

    if target_id.is_empty() {
        return Err(AppError::invalid_payload("targetId is required"));
    }
    validate_backup_id(backup_id)?;
    let target_root = resolve_target_root(target_id)?;
    let catalog = catalog_state(&target_root)?;
    let receipt_map: Map<String, Value> = match catalog.get(backup_id).and_then(Value::as_object) {
        Some(obj) => obj.clone(),
        None => {
            let rp = receipt_path(&target_root, backup_id)?;
            if !rp.is_file() {
                return Err(AppError::not_found("Backup receipt not found on target"));
            }
            read_json_object(&rp)?
        }
    };

    let catalog_hash: HashMap<String, Value> = catalog
        .iter()
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
    let markers = target_markers(&target_root);
    let now_secs = now_unix_secs();
    let now_iso = utc_iso_from_unix(now_secs);
    let expires_at = utc_iso_from_unix(now_secs + HOLD_TTL_SECONDS);

    if receipt_map.get("storageProtocol").and_then(Value::as_str) == Some(OBJECT_SET_V1) {
        let skind = truthy_str(receipt_map.get("snapshotKind"));
        let chain_raw = if skind == "incremental" {
            resolve_lineage_from_receipts(&catalog_hash, backup_id)?
        } else {
            vec![Value::Object(receipt_map.clone())]
        };
        let mut verified_receipts = Vec::with_capacity(chain_raw.len());
        for raw_member in &chain_raw {
            let m_id = truthy_str(raw_member.get("backupId"));
            let rp = receipt_path(&target_root, &m_id)?;
            let raw_bytes = fs::read(&rp)
                .map_err(|_| conflict_err(format!("Backup {m_id} has no immutable receipt")))?;
            let committed: Map<String, Value> = serde_json::from_slice(&raw_bytes)
                .map_err(|_| conflict_err(format!("Backup {m_id} has no immutable receipt")))?;
            if committed.get("storageProtocol").and_then(Value::as_str) != Some(OBJECT_SET_V1) {
                return Err(conflict_err(
                    "Object-set lineage cannot reference Whole-Age ancestors",
                ));
            }
            let _ = committed_object_inventory(&committed)?;
            let set_digest = truthy_str(committed.get("objectSetDigest"));
            let ctrl_digest = truthy_str(committed.get("controlObjectDigest"));
            let receipt_digest = sha256_hex(&raw_bytes);
            let committed_ok = markers.iter().any(|cand| {
                cand.get("schemaVersion").and_then(Value::as_i64) == Some(4)
                    && cand.get("storageProtocol").and_then(Value::as_str) == Some(OBJECT_SET_V1)
                    && truthy_str(cand.get("backupId")) == m_id
                    && truthy_str(cand.get("objectSetDigest")) == set_digest
                    && truthy_str(cand.get("controlObjectDigest")) == ctrl_digest
                    && truthy_str(cand.get("receiptDigest")) == receipt_digest
                    && commit_marker_valid(cand)
            });
            if !committed_ok {
                return Err(conflict_err(format!(
                    "Backup {m_id} has no formal object-set commit"
                )));
            }
            verified_receipts.push(committed);
        }

        let restore_id = random_restore_id();
        let staging_root = root.join(".restore-staging").join(&restore_id);
        fs::create_dir_all(&staging_root).map_err(|_| route_support::unhandled_error())?;

        let mut members = Vec::with_capacity(verified_receipts.len());
        for (idx, r) in verified_receipts.iter().enumerate() {
            members.push(object_set_member_descriptor(
                &target_root,
                r,
                &staging_root,
                idx,
            )?);
        }
        let mut hold_keys = Vec::with_capacity(members.len());
        for (idx, member) in members.iter().enumerate() {
            let hold = json!({
                "schemaVersion": 3,
                "storageProtocol": OBJECT_SET_V1,
                "restoreId": restore_id,
                "backupId": member["backupId"],
                "objectSetDigest": member["objectSetDigest"],
                "objects": member["objects"],
                "targetId": target_id,
                "createdAt": now_iso,
                "generation": 0,
                "expiresAt": expires_at,
            });
            let hk = format!("holds/restore/{restore_id}-{idx}.json");
            put_json_if_absent(&target_root, &hk, &hold)?;
            hold_keys.push(hk);
        }
        let effective_skind = if skind.is_empty() {
            "full".to_string()
        } else {
            skind
        };
        let mut session = Map::new();
        session.insert("schemaVersion".into(), json!(SESSION_SCHEMA_VERSION));
        session.insert(
            "storageProtocol".into(),
            Value::String(OBJECT_SET_V1.into()),
        );
        session.insert("restoreId".into(), Value::String(restore_id.clone()));
        session.insert("source".into(), Value::String("remote-target".into()));
        session.insert("targetId".into(), Value::String(target_id.into()));
        session.insert(
            "activeSourceTargetId".into(),
            Value::String(target_id.into()),
        );
        session.insert("attemptedSourceTargets".into(), json!([target_id]));
        session.insert("failoverCount".into(), json!(0));
        session.insert("targetSwitchEvents".into(), json!([]));
        session.insert("backupId".into(), Value::String(backup_id.into()));
        session.insert(
            "snapshotKind".into(),
            Value::String(effective_skind.clone()),
        );
        session.insert("chain".into(), Value::Array(members.clone()));
        session.insert("controlIndex".into(), json!(0));
        session.insert("holdKeys".into(), json!(hold_keys));
        session.insert(
            "selection".into(),
            selection
                .as_ref()
                .map_or(Value::Null, RestoreSelection::canonical_value),
        );
        session.insert(
            "selectionDigest".into(),
            selection_digest.clone().map_or(Value::Null, Value::String),
        );
        session.insert("phase".into(), Value::String("fetching-controls".into()));
        session.insert("createdAt".into(), Value::String(now_iso.clone()));
        session.insert("lastHoldRenewedAt".into(), Value::String(now_iso.clone()));
        session.insert("updatedAt".into(), Value::String(now_iso));
        let sp = remote_session_path(root, &restore_id)?;
        write_atomic_remote_session(&sp, &mut session, false, false, false)?;
        let chain_ids: Vec<Value> = members.iter().map(|m| m["backupId"].clone()).collect();
        return Ok(json!({
            "restoreId": restore_id,
            "storageProtocol": OBJECT_SET_V1,
            "snapshotKind": effective_skind,
            "chain": chain_ids,
            "phase": "fetching-controls",
            "targetId": target_id,
            "activeSourceTargetId": target_id,
            "backupId": backup_id,
            "selection": session["selection"],
            "selectionDigest": session["selectionDigest"],
            "holds": hold_keys,
        }));
    }

    // Whole-Age v1
    let digest = truthy_str(
        receipt_map
            .get("objectDigest")
            .or_else(|| receipt_map.get("ciphertextSha256")),
    );
    if !is_plain_sha256(&digest) {
        return Err(conflict_err("Backup receipt is missing object digest"));
    }
    let has_commit = markers.iter().any(|m| {
        truthy_str(m.get("backupId")) == backup_id && truthy_str(m.get("objectDigest")) == digest
    });
    if !has_commit {
        return Err(conflict_err("Backup has no formal slot commit on target"));
    }

    let restore_id = random_restore_id();
    let staging_root = root.join(".restore-staging").join(&restore_id);
    fs::create_dir_all(&staging_root).map_err(|_| route_support::unhandled_error())?;

    let skind = {
        let s = truthy_str(receipt_map.get("snapshotKind"));
        if s.is_empty() { "full".to_string() } else { s }
    };
    if skind == "incremental" {
        let chain_raw = resolve_lineage_from_receipts(&catalog_hash, backup_id)?;
        let chain_ids: Vec<String> = chain_raw
            .iter()
            .map(|r| truthy_str(r.get("backupId")))
            .collect();
        if !chain_ids.iter().any(|id| id == backup_id) || chain_ids.is_empty() {
            return Err(conflict_err(
                "Incremental chain cannot be resolved on target",
            ));
        }
        let mut members = Vec::with_capacity(chain_raw.len());
        for item in &chain_raw {
            members.push(whole_age_chain_member(&target_root, item, &staging_root)?);
        }
        let mut hold_keys = Vec::with_capacity(members.len());
        for (idx, member) in members.iter().enumerate() {
            let hold = json!({
                "schemaVersion": 3,
                "restoreId": restore_id,
                "backupId": member["backupId"],
                "objectDigest": member["objectDigest"],
                "targetId": target_id,
                "createdAt": now_iso,
                "generation": 0,
                "expiresAt": expires_at,
            });
            let hk = format!("holds/restore/{restore_id}-{idx}.json");
            let _ = put_json_if_absent(&target_root, &hk, &hold);
            hold_keys.push(hk);
        }
        let mut session = Map::new();
        session.insert("schemaVersion".into(), json!(SESSION_SCHEMA_VERSION));
        session.insert("restoreId".into(), Value::String(restore_id.clone()));
        session.insert("source".into(), Value::String("remote-target".into()));
        session.insert("targetId".into(), Value::String(target_id.into()));
        session.insert("backupId".into(), Value::String(backup_id.into()));
        session.insert("snapshotKind".into(), Value::String("incremental".into()));
        session.insert("chain".into(), Value::Array(members));
        session.insert("chainIndex".into(), json!(0));
        session.insert("holdKeys".into(), json!(hold_keys));
        session.insert(
            "selection".into(),
            selection
                .as_ref()
                .map_or(Value::Null, RestoreSelection::canonical_value),
        );
        session.insert(
            "selectionDigest".into(),
            selection_digest.clone().map_or(Value::Null, Value::String),
        );
        session.insert("phase".into(), Value::String("fetching-chain".into()));
        session.insert("createdAt".into(), Value::String(now_iso.clone()));
        session.insert("lastHoldRenewedAt".into(), Value::String(now_iso.clone()));
        session.insert("updatedAt".into(), Value::String(now_iso));
        let sp = remote_session_path(root, &restore_id)?;
        write_atomic_remote_session(&sp, &mut session, false, false, false)?;
        return Ok(json!({
            "restoreId": restore_id,
            "snapshotKind": "incremental",
            "chain": chain_ids,
            "phase": "fetching-chain",
            "targetId": target_id,
            "backupId": backup_id,
            "selection": session["selection"],
            "selectionDigest": session["selectionDigest"],
            "holds": hold_keys,
        }));
    }

    let obj_file = object_path(&target_root, &digest)?;
    let Ok(obj_meta) = fs::metadata(&obj_file) else {
        return Err(AppError::not_found(
            "Backup ciphertext object is missing on target",
        ));
    };
    let hk = format!("holds/restore/{restore_id}.json");
    let hold = json!({
        "schemaVersion": 3,
        "restoreId": restore_id,
        "backupId": backup_id,
        "objectDigest": digest,
        "targetId": target_id,
        "createdAt": now_iso,
        "generation": 0,
        "expiresAt": expires_at,
    });
    let _ = put_json_if_absent(&target_root, &hk, &hold);
    let filename = {
        let f = truthy_str(receipt_map.get("filename"));
        if f.is_empty() {
            format!("{backup_id}.age")
        } else {
            f
        }
    };
    let cipher_path = staging_root.join(&filename);
    let mut session = Map::new();
    session.insert("schemaVersion".into(), json!(SESSION_SCHEMA_VERSION));
    session.insert("restoreId".into(), Value::String(restore_id.clone()));
    session.insert("source".into(), Value::String("remote-target".into()));
    session.insert("targetId".into(), Value::String(target_id.into()));
    session.insert("backupId".into(), Value::String(backup_id.into()));
    session.insert("objectDigest".into(), Value::String(digest.clone()));
    session.insert("filename".into(), Value::String(filename));
    session.insert("expectedBytes".into(), json!(obj_meta.len()));
    session.insert("downloadedBytes".into(), json!(0));
    session.insert("remoteETag".into(), Value::Null);
    session.insert("remoteVersionId".into(), Value::Null);
    session.insert("holdKey".into(), Value::String(hk));
    session.insert(
        "selection".into(),
        selection
            .as_ref()
            .map_or(Value::Null, RestoreSelection::canonical_value),
    );
    session.insert(
        "selectionDigest".into(),
        selection_digest.clone().map_or(Value::Null, Value::String),
    );
    session.insert(
        "ciphertextPath".into(),
        Value::String(cipher_path.to_string_lossy().into_owned()),
    );
    session.insert("phase".into(), Value::String("fetching".into()));
    session.insert("createdAt".into(), Value::String(now_iso.clone()));
    session.insert("lastHoldRenewedAt".into(), Value::String(now_iso.clone()));
    session.insert("updatedAt".into(), Value::String(now_iso));
    let sp = remote_session_path(root, &restore_id)?;
    write_atomic_remote_session(&sp, &mut session, false, false, false)?;
    Ok(json!({
        "restoreId": restore_id,
        "phase": "fetching",
        "downloadedBytes": 0,
        "expectedBytes": obj_meta.len(),
        "targetId": target_id,
        "backupId": backup_id,
        "objectDigest": digest,
        "selection": session["selection"],
        "selectionDigest": session["selectionDigest"],
        "hold": hold,
    }))
}

fn renew_session_holds(target_root: &Path, session: &mut Map<String, Value>) {
    let now_secs = now_unix_secs();
    let expires_at = utc_iso_from_unix(now_secs + HOLD_TTL_SECONDS);
    let mut keys: Vec<String> = session
        .get("holdKeys")
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .filter_map(|v| v.as_str().map(str::to_string))
                .collect()
        })
        .unwrap_or_default();
    if keys.is_empty() {
        let hk = truthy_str(session.get("holdKey"));
        if !hk.is_empty() {
            keys.push(hk);
        }
    }
    for key in keys {
        let path = hold_path(target_root, &key);
        if let Ok(mut hold) = read_json_object(&path) {
            hold.insert("expiresAt".into(), Value::String(expires_at.clone()));
            let _ = write_atomic_json(&path, &Value::Object(hold));
        }
    }
    session.insert(
        "lastHoldRenewedAt".into(),
        Value::String(utc_iso_from_unix(now_secs)),
    );
}

fn increment_telemetry_counter(session: &mut Map<String, Value>, key: &str, amount: u64) {
    if amount == 0 {
        return;
    }
    let mut tele = session
        .get("recoveryTelemetry")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let mut counters = tele
        .get("counters")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let cur = counters.get(key).and_then(Value::as_u64).unwrap_or(0);
    counters.insert(key.to_string(), json!(cur.saturating_add(amount)));
    tele.insert("schemaVersion".into(), json!(1));
    tele.insert("counters".into(), Value::Object(counters));
    session.insert("recoveryTelemetry".into(), Value::Object(tele));
}

fn record_telemetry_stage(
    session: &mut Map<String, Value>,
    stage: &str,
    result: &str,
    duration_ms: u128,
    byte_count: u64,
    components: usize,
) {
    let mut tele = session
        .get("recoveryTelemetry")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let mut samples = tele
        .get("samples")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    samples.push(json!({
        "stage": stage,
        "result": result,
        "durationMs": duration_ms as u64,
        "bytes": byte_count,
        "components": components,
        "observedAt": now_utc_iso(),
    }));
    if samples.len() > 64 {
        samples = samples.split_off(samples.len() - 64);
    }
    tele.insert("schemaVersion".into(), json!(1));
    tele.insert("samples".into(), Value::Array(samples));
    session.insert("recoveryTelemetry".into(), Value::Object(tele));
}

fn download_local_object_member(
    target_root: &Path,
    member: &mut Map<String, Value>,
    max_bytes: Option<u64>,
    session: &mut Map<String, Value>,
) -> Result<bool, AppError> {
    let digest = truthy_str(member.get("objectDigest"));
    let src_path = object_path(target_root, &digest)?;
    let expected = member
        .get("expectedBytes")
        .and_then(Value::as_u64)
        .unwrap_or(0);
    let dst_str = truthy_str(member.get("ciphertextPath"));
    let dst_path = PathBuf::from(&dst_str);
    if let Some(parent) = dst_path.parent() {
        fs::create_dir_all(parent).map_err(|_| route_support::unhandled_error())?;
    }

    let started = Instant::now();
    let offset = if dst_path.is_file() {
        fs::metadata(&dst_path).map(|m| m.len()).unwrap_or(0)
    } else {
        0
    };
    let remaining = expected.saturating_sub(offset);
    let budget = match max_bytes {
        Some(limit) => remaining.min(limit),
        None => remaining,
    };

    let mut hasher = Sha256::new();
    if offset > 0 {
        let mut ex = File::open(&dst_path).map_err(|_| route_support::unhandled_error())?;
        let mut buf = vec![0_u8; 256 * 1024];
        loop {
            let n = ex
                .read(&mut buf)
                .map_err(|_| route_support::unhandled_error())?;
            if n == 0 {
                break;
            }
            hasher.update(&buf[..n]);
        }
    }

    let mut transferred = 0_u64;
    if budget > 0 {
        let mut src = File::open(&src_path).map_err(|_| {
            AppError::not_found(format!(
                "Backup object is missing on target: {}",
                truthy_str(member.get("backupId"))
            ))
        })?;
        src.seek(SeekFrom::Start(offset))
            .map_err(|_| route_support::unhandled_error())?;
        let mut out = OpenOptions::new()
            .write(true)
            .create(true)
            .append(offset > 0)
            .truncate(offset == 0)
            .open(&dst_path)
            .map_err(|_| route_support::unhandled_error())?;
        let mut left = budget;
        let mut buf = vec![0_u8; 256 * 1024];
        while left > 0 {
            let want = (left as usize).min(buf.len());
            let n = src
                .read(&mut buf[..want])
                .map_err(|_| route_support::unhandled_error())?;
            if n == 0 {
                break;
            }
            out.write_all(&buf[..n])
                .map_err(|_| route_support::unhandled_error())?;
            hasher.update(&buf[..n]);
            left -= n as u64;
            transferred += n as u64;
        }
        out.flush().map_err(|_| route_support::unhandled_error())?;
        out.sync_all()
            .map_err(|_| route_support::unhandled_error())?;
    }

    let downloaded = fs::metadata(&dst_path).map(|m| m.len()).unwrap_or(0);
    member.insert("downloadedBytes".into(), json!(downloaded));
    if transferred > 0 {
        increment_telemetry_counter(session, "transferBytes", transferred);
        increment_telemetry_counter(session, "componentsTransferred", 1);
        if offset > 0 {
            increment_telemetry_counter(session, "transferRetry", 1);
        }
    }
    let done = downloaded >= expected;
    if done {
        let actual = format!("{:x}", hasher.finalize());
        if actual != digest {
            increment_telemetry_counter(session, "integrityFailure", 1);
            record_telemetry_stage(
                session,
                "transfer",
                "failed",
                started.elapsed().as_millis(),
                transferred,
                usize::from(transferred > 0),
            );
            return Err(AppError {
                message: format!(
                    "Downloaded backup digest mismatch: {}",
                    truthy_str(member.get("backupId"))
                ),
                code: codes::INTERNAL,
                status: 500,
            });
        }
        member.insert("fetched".into(), json!(true));
    }
    record_telemetry_stage(
        session,
        "transfer",
        "success",
        started.elapsed().as_millis(),
        transferred,
        usize::from(transferred > 0),
    );
    Ok(done)
}

fn update_component_state(
    session: &mut Map<String, Value>,
    component: &Value,
    state: &str,
    downloaded_bytes: u64,
) {
    let digest = truthy_str(component.get("objectDigest"));
    let mut states = session
        .get("componentStates")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let mut entry = states
        .get(&digest)
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    entry.insert("state".into(), Value::String(state.into()));
    entry.insert("downloadedBytes".into(), json!(downloaded_bytes));
    entry.insert(
        "expectedBytes".into(),
        json!(
            component
                .get("expectedBytes")
                .and_then(Value::as_u64)
                .unwrap_or(0)
        ),
    );
    states.insert(digest, Value::Object(entry));
    session.insert("componentStates".into(), Value::Object(states));
}

fn fetch_object_set_controls(
    root: &Path,
    target_root: &Path,
    restore_id: &str,
    session: &mut Map<String, Value>,
    max_bytes: Option<u64>,
) -> Result<Value, AppError> {
    let mut chain = restore_members(session)?;
    let mut index = session
        .get("controlIndex")
        .and_then(Value::as_u64)
        .unwrap_or(0) as usize;
    let sp = remote_session_path(root, restore_id)?;
    while index < chain.len() {
        let mut ctrl = chain[index]
            .get("control")
            .and_then(Value::as_object)
            .cloned()
            .ok_or_else(|| conflict_err("Object-set restore control descriptor is invalid"))?;
        let digest = truthy_str(ctrl.get("objectDigest"));
        let obj_file = object_path(target_root, &digest)?;
        if !obj_file.is_file() {
            return Err(AppError::not_found(format!(
                "Backup control object is missing on target: {}",
                truthy_str(chain[index].get("backupId"))
            )));
        }
        let done = download_local_object_member(target_root, &mut ctrl, max_bytes, session)?;
        let dl_bytes = ctrl
            .get("downloadedBytes")
            .and_then(Value::as_u64)
            .unwrap_or(0);
        let exp_bytes = ctrl
            .get("expectedBytes")
            .and_then(Value::as_u64)
            .unwrap_or(0);
        if let Some(m_obj) = chain[index].as_object_mut() {
            m_obj.insert("control".into(), Value::Object(ctrl));
        }
        session.insert("chain".into(), Value::Array(chain.clone()));
        session.insert("updatedAt".into(), Value::String(now_utc_iso()));
        if !done {
            session.insert("controlIndex".into(), json!(index));
            session.insert("phase".into(), Value::String("fetching-controls".into()));
            write_atomic_remote_session(&sp, session, false, false, false)?;
            return Ok(json!({
                "restoreId": restore_id,
                "phase": "fetching-controls",
                "backupId": chain[index]["backupId"],
                "downloadedBytes": dl_bytes,
                "expectedBytes": exp_bytes,
            }));
        }
        index += 1;
        session.insert("controlIndex".into(), json!(index));
        write_atomic_remote_session(&sp, session, false, false, false)?;
    }

    let dl_sum: u64 = chain
        .iter()
        .map(|m| m["control"]["downloadedBytes"].as_u64().unwrap_or(0))
        .sum();
    let exp_sum: u64 = chain
        .iter()
        .map(|m| m["control"]["expectedBytes"].as_u64().unwrap_or(0))
        .sum();
    session.insert("controlIndex".into(), json!(index));
    session.insert("phase".into(), Value::String("controls-fetched".into()));
    session.insert("downloadedBytes".into(), json!(dl_sum));
    session.insert("expectedBytes".into(), json!(exp_sum));
    write_atomic_remote_session(&sp, session, false, false, false)?;
    Ok(json!({
        "restoreId": restore_id,
        "phase": "controls-fetched",
        "downloadedBytes": dl_sum,
        "expectedBytes": exp_sum,
        "controlObjects": chain.len(),
        "next": "unlock-and-plan-projection",
    }))
}

fn fetch_object_set_components(
    root: &Path,
    target_root: &Path,
    restore_id: &str,
    session: &mut Map<String, Value>,
    max_bytes: Option<u64>,
) -> Result<Value, AppError> {
    let cache_root = component_cache_dir(root);
    let sp = remote_session_path(root, restore_id)?;
    let mut chain = restore_members(session)?;
    let mut req_count = 0usize;

    for member in &mut chain {
        let Some(reqs) = member
            .get_mut("requiredComponents")
            .and_then(Value::as_array_mut)
        else {
            continue;
        };
        for comp in reqs {
            req_count += 1;
            if comp.get("fetched").and_then(Value::as_bool) == Some(true) {
                continue;
            }
            let digest = truthy_str(comp.get("objectDigest"));
            let exp_bytes = comp
                .get("expectedBytes")
                .and_then(Value::as_u64)
                .unwrap_or(0);
            let obj_file = object_path(target_root, &digest)?;
            let src_meta = fs::metadata(&obj_file).ok();
            if src_meta.is_none_or(|m| m.len() != exp_bytes) {
                return Err(AppError::not_found(
                    "Required object-set component is missing",
                ));
            }

            if max_bytes.is_none() {
                let cache_existed = component_cache_path(&cache_root, &digest).is_file();
                if inspect_cached_component(&cache_root, &digest, exp_bytes) {
                    increment_telemetry_counter(session, "cacheHit", 1);
                    let cached_path = component_cache_path(&cache_root, &digest);
                    if let Some(cobj) = comp.as_object_mut() {
                        cobj.insert(
                            "ciphertextPath".into(),
                            Value::String(cached_path.to_string_lossy().into_owned()),
                        );
                        cobj.insert("downloadedBytes".into(), json!(exp_bytes));
                        cobj.insert("fetched".into(), json!(true));
                    }
                    update_component_state(session, comp, "verified", exp_bytes);
                    continue;
                }
                increment_telemetry_counter(
                    session,
                    if cache_existed {
                        "cacheCorruption"
                    } else {
                        "cacheMiss"
                    },
                    1,
                );
                let partial = component_cache_partial_path(&cache_root, &digest);
                if let Some(cobj) = comp.as_object_mut() {
                    cobj.insert(
                        "ciphertextPath".into(),
                        Value::String(partial.to_string_lossy().into_owned()),
                    );
                }
                let mut c_map = comp.as_object().cloned().unwrap_or_default();
                let done = download_local_object_member(target_root, &mut c_map, None, session)?;
                let final_cache = component_cache_path(&cache_root, &digest);
                if let Some(parent) = final_cache.parent() {
                    fs::create_dir_all(parent).map_err(|_| route_support::unhandled_error())?;
                }
                if done {
                    fs::rename(&partial, &final_cache)
                        .map_err(|_| route_support::unhandled_error())?;
                    c_map.insert(
                        "ciphertextPath".into(),
                        Value::String(final_cache.to_string_lossy().into_owned()),
                    );
                    update_component_state(
                        session,
                        &Value::Object(c_map.clone()),
                        "verified",
                        exp_bytes,
                    );
                }
                *comp = Value::Object(c_map);
            } else {
                let mut c_map = comp.as_object().cloned().unwrap_or_default();
                let dl0 = c_map
                    .get("downloadedBytes")
                    .and_then(Value::as_u64)
                    .unwrap_or(0);
                update_component_state(session, &Value::Object(c_map.clone()), "downloading", dl0);
                let done =
                    download_local_object_member(target_root, &mut c_map, max_bytes, session)?;
                let dl1 = c_map
                    .get("downloadedBytes")
                    .and_then(Value::as_u64)
                    .unwrap_or(0);
                update_component_state(
                    session,
                    &Value::Object(c_map.clone()),
                    if done { "verified" } else { "partial" },
                    dl1,
                );
                *comp = Value::Object(c_map);
                if !done {
                    session.insert("chain".into(), Value::Array(chain.clone()));
                    session.insert(
                        "phase".into(),
                        Value::String("fetching-selected-components".into()),
                    );
                    session.insert("updatedAt".into(), Value::String(now_utc_iso()));
                    write_atomic_remote_session(&sp, session, false, false, false)?;
                    return Ok(json!({
                        "restoreId": restore_id,
                        "phase": "fetching-selected-components",
                        "downloadedBytes": dl1,
                        "expectedBytes": exp_bytes,
                        "requiredComponents": req_count,
                    }));
                }
            }
        }
    }

    let ctrl_bytes: u64 = chain
        .iter()
        .map(|m| m["control"]["downloadedBytes"].as_u64().unwrap_or(0))
        .sum();
    let mut comp_dl = 0_u64;
    let mut comp_exp = 0_u64;
    for member in &chain {
        if let Some(reqs) = member.get("requiredComponents").and_then(Value::as_array) {
            for comp in reqs {
                comp_dl += comp
                    .get("downloadedBytes")
                    .and_then(Value::as_u64)
                    .unwrap_or(0);
                comp_exp += comp
                    .get("expectedBytes")
                    .and_then(Value::as_u64)
                    .unwrap_or(0);
            }
        }
    }
    session.insert("chain".into(), Value::Array(chain));
    session.insert("phase".into(), Value::String("components-fetched".into()));
    session.insert("downloadedBytes".into(), json!(ctrl_bytes + comp_dl));
    session.insert("expectedBytes".into(), json!(ctrl_bytes + comp_exp));
    session.insert("updatedAt".into(), Value::String(now_utc_iso()));
    write_atomic_remote_session(&sp, session, false, false, false)?;
    Ok(json!({
        "restoreId": restore_id,
        "phase": "components-fetched",
        "downloadedBytes": ctrl_bytes + comp_dl,
        "expectedBytes": ctrl_bytes + comp_exp,
        "requiredComponents": req_count,
        "next": "decrypt-and-materialize",
    }))
}

pub(crate) fn fetch_restore_session(
    root: &Path,
    restore_id: &str,
    max_bytes: Option<u64>,
) -> Result<Value, AppError> {
    validate_restore_id(restore_id)?;
    let Some(mut session) = read_remote_restore_session(root, restore_id)? else {
        return Err(AppError::not_found("Remote restore session not found"));
    };
    let sp = remote_session_path(root, restore_id)?;
    let session_dir = sp
        .parent()
        .ok_or_else(route_support::unhandled_error)?
        .to_path_buf();
    let controlled = converge_job_control(root, restore_id, &session_dir, &sp, &mut session)?;
    if matches!(
        controlled.as_str(),
        "paused" | "aborted" | "rolled-back" | "recovery-required"
    ) {
        return Ok(json!({"restoreId": restore_id, "phase": controlled}));
    }
    let target_id = truthy_str(session.get("targetId"));
    let target_root = resolve_target_root(&target_id)?;
    renew_session_holds(&target_root, &mut session);

    let phase = truthy_str(session.get("phase"));
    if matches!(
        phase.as_str(),
        "fetched" | "chain-fetched" | "controls-fetched" | "components-fetched"
    ) {
        return Ok(json!({
            "restoreId": restore_id,
            "phase": phase,
            "downloadedBytes": session.get("downloadedBytes").and_then(Value::as_u64).unwrap_or(0),
            "expectedBytes": session.get("expectedBytes").and_then(Value::as_u64).unwrap_or(0),
            "path": truthy_str(session.get("ciphertextPath")),
            "next": "inspect-or-unlock",
        }));
    }

    if session.get("storageProtocol").and_then(Value::as_str) == Some(OBJECT_SET_V1) {
        if matches!(
            phase.as_str(),
            "fetching-selected-components" | "components-fetched" | "decrypting-components"
        ) {
            return fetch_object_set_components(
                root,
                &target_root,
                restore_id,
                &mut session,
                max_bytes,
            );
        }
        return fetch_object_set_controls(root, &target_root, restore_id, &mut session, max_bytes);
    }

    if truthy_str(session.get("snapshotKind")) == "incremental" {
        let mut chain = session
            .get("chain")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let mut index = session
            .get("chainIndex")
            .and_then(Value::as_u64)
            .unwrap_or(0) as usize;
        while index < chain.len() {
            let mut m_map = chain[index]
                .as_object()
                .cloned()
                .ok_or_else(|| conflict_err("Incremental chain member is invalid"))?;
            let digest = truthy_str(m_map.get("objectDigest"));
            if !object_path(&target_root, &digest)?.is_file() {
                return Err(AppError::not_found(format!(
                    "Backup ciphertext object is missing on target: {}",
                    truthy_str(m_map.get("backupId"))
                )));
            }
            let done =
                download_local_object_member(&target_root, &mut m_map, max_bytes, &mut session)?;
            chain[index] = Value::Object(m_map.clone());
            session.insert("chain".into(), Value::Array(chain.clone()));
            session.insert("updatedAt".into(), Value::String(now_utc_iso()));
            if !done {
                session.insert("chainIndex".into(), json!(index));
                session.insert("phase".into(), Value::String("fetching-chain".into()));
                write_atomic_remote_session(&sp, &mut session, false, false, false)?;
                let chain_ids: Vec<Value> = chain.iter().map(|m| m["backupId"].clone()).collect();
                return Ok(json!({
                    "restoreId": restore_id,
                    "phase": "fetching-chain",
                    "backupId": m_map["backupId"],
                    "downloadedBytes": m_map["downloadedBytes"],
                    "expectedBytes": m_map["expectedBytes"],
                    "path": m_map["ciphertextPath"],
                    "chain": chain_ids,
                }));
            }
            index += 1;
        }
        let dl_sum: u64 = chain
            .iter()
            .map(|m| m["downloadedBytes"].as_u64().unwrap_or(0))
            .sum();
        let exp_sum: u64 = chain
            .iter()
            .map(|m| m["expectedBytes"].as_u64().unwrap_or(0))
            .sum();
        session.insert("chainIndex".into(), json!(index));
        session.insert("phase".into(), Value::String("chain-fetched".into()));
        session.insert("downloadedBytes".into(), json!(dl_sum));
        session.insert("expectedBytes".into(), json!(exp_sum));
        write_atomic_remote_session(&sp, &mut session, false, false, false)?;
        let chain_ids: Vec<Value> = chain.iter().map(|m| m["backupId"].clone()).collect();
        let cpaths: Vec<Value> = chain.iter().map(|m| m["ciphertextPath"].clone()).collect();
        return Ok(json!({
            "restoreId": restore_id,
            "phase": "chain-fetched",
            "downloadedBytes": dl_sum,
            "expectedBytes": exp_sum,
            "chain": chain_ids,
            "ciphertextPaths": cpaths,
            "next": "inspect-or-unlock",
        }));
    }

    let digest = truthy_str(session.get("objectDigest"));
    let obj_file = object_path(&target_root, &digest)?;
    let Ok(obj_meta) = fs::metadata(&obj_file) else {
        return Err(AppError::not_found(
            "Backup ciphertext object is missing on target",
        ));
    };
    let cipher_str = {
        let existing = truthy_str(session.get("ciphertextPath"));
        if existing.is_empty() {
            let fname = {
                let f = truthy_str(session.get("filename"));
                if f.is_empty() {
                    "package.age".to_string()
                } else {
                    f
                }
            };
            session_dir.join(fname).to_string_lossy().into_owned()
        } else {
            existing
        }
    };
    let mut member = Map::new();
    member.insert(
        "backupId".into(),
        Value::String(truthy_str(session.get("backupId"))),
    );
    member.insert("objectDigest".into(), Value::String(digest.clone()));
    member.insert(
        "expectedBytes".into(),
        json!(
            session
                .get("expectedBytes")
                .and_then(Value::as_u64)
                .unwrap_or_else(|| obj_meta.len())
        ),
    );
    member.insert("ciphertextPath".into(), Value::String(cipher_str.clone()));
    member.insert(
        "downloadedBytes".into(),
        json!(
            session
                .get("downloadedBytes")
                .and_then(Value::as_u64)
                .unwrap_or(0)
        ),
    );
    let done = download_local_object_member(&target_root, &mut member, max_bytes, &mut session)?;
    let dl = member
        .get("downloadedBytes")
        .and_then(Value::as_u64)
        .unwrap_or(0);
    let exp = member
        .get("expectedBytes")
        .and_then(Value::as_u64)
        .unwrap_or(0);
    session.insert("downloadedBytes".into(), json!(dl));
    session.insert("updatedAt".into(), Value::String(now_utc_iso()));
    let out_phase = if done { "fetched" } else { "fetching" };
    session.insert("phase".into(), Value::String(out_phase.into()));
    write_atomic_remote_session(&sp, &mut session, false, false, false)?;
    Ok(json!({
        "restoreId": restore_id,
        "phase": out_phase,
        "downloadedBytes": dl,
        "expectedBytes": exp,
        "path": cipher_str,
        "objectDigest": digest,
        "next": if done { json!("inspect-or-unlock") } else { Value::Null },
    }))
}

fn decrypt_by_kind(src: &Path, dst: &Path, kind: &str, secret: &str) -> Result<(), AppError> {
    if kind == "passphrase" {
        decrypt_passphrase_file_native(src, dst, secret)
    } else {
        decrypt_file_native(src, dst, secret)
    }
}

fn build_chain_packages(
    decrypted_paths: &[PathBuf],
    base: &Path,
) -> Result<Vec<ChainPackage>, AppError> {
    let mut packages = Vec::with_capacity(decrypted_paths.len());
    for (idx, decrypted) in decrypted_paths.iter().enumerate() {
        let meta_dir = base.join(format!("metadata-{idx}"));
        let manifest = extract_archive_metadata(decrypted, &meta_dir)?;
        let skind = truthy_str(manifest.get("snapshotKind"));
        let snapshot_kind = if skind.is_empty() {
            "full".into()
        } else {
            skind
        };
        let mut operations = None;
        let mut pack_index = None;
        if snapshot_kind == "incremental" {
            let ops_path = meta_dir.join("delta").join("operations.json");
            if ops_path.is_file() {
                operations = read_json_object(&ops_path).ok().map(Value::Object);
            }
            let idx_path = meta_dir.join(PACK_INDEX_PATH);
            if idx_path.is_file() {
                pack_index = read_json_object(&idx_path).ok().map(Value::Object);
            }
        }
        let contributor_ids: BTreeSet<String> = manifest
            .get("contributors")
            .and_then(Value::as_array)
            .map(|arr| {
                arr.iter()
                    .filter_map(|c| c.get("id").and_then(Value::as_str).map(str::to_string))
                    .filter(|s| !s.is_empty())
                    .collect()
            })
            .unwrap_or_default();
        let frontend = manifest.get("frontend").is_some_and(python_truthy);
        let external_mcp = manifest
            .get("coverage")
            .and_then(|c| c.get("externalContributors"))
            .is_some_and(python_truthy);
        let files = manifest_files(&manifest)?;
        let root_digest = truthy_str(manifest.get("snapshot").and_then(|s| s.get("rootDigest")));
        packages.push(ChainPackage {
            snapshot_kind,
            files,
            root_digest,
            operations,
            pack_index,
            frontend,
            contributor_ids,
            external_mcp,
            manifest,
        });
    }
    Ok(packages)
}

fn plan_object_set_projection(
    root: &Path,
    session: &mut Map<String, Value>,
    kind: &str,
    secret_str: &str,
) -> Result<(Option<ProjectionPlan>, Value), AppError> {
    let restore_id = truthy_str(session.get("restoreId"));
    let sp = remote_session_path(root, &restore_id)?;
    let base = sp
        .parent()
        .ok_or_else(route_support::unhandled_error)?
        .to_path_buf();
    let mut members = restore_members(session)?;
    session.insert("phase".into(), Value::String("decrypting-controls".into()));
    session.insert("updatedAt".into(), Value::String(now_utc_iso()));
    write_atomic_remote_session(&sp, session, false, false, false)?;

    let mut decrypted_controls = Vec::with_capacity(members.len());
    for (idx, member) in members.iter().enumerate() {
        let cipher_str = truthy_str(member.get("control").and_then(|c| c.get("ciphertextPath")));
        let cipher = PathBuf::from(&cipher_str);
        if !cipher.is_file() {
            return Err(AppError::not_found(
                "Object-set control ciphertext is unavailable",
            ));
        }
        let decrypted = base.join(format!("control-decrypted-{idx}.zip"));
        decrypt_by_kind(&cipher, &decrypted, kind, secret_str)?;
        decrypted_controls.push(decrypted);
    }

    session.insert("phase".into(), Value::String("planning-projection".into()));
    session.insert("updatedAt".into(), Value::String(now_utc_iso()));
    write_atomic_remote_session(&sp, session, false, false, false)?;

    let packages = build_chain_packages(&decrypted_controls, &base)?;
    for (idx, (member, package)) in members.iter().zip(packages.iter()).enumerate() {
        verify_control_metadata(&base.join(format!("metadata-{idx}")))?;
        let m_bid = truthy_str(member.get("backupId"));
        let p_bid = truthy_str(package.manifest.get("backupId"));
        let m_sk = truthy_str(member.get("snapshotKind"));
        let p_sk = truthy_str(package.manifest.get("snapshotKind"));
        let p_sp = truthy_str(package.manifest.get("storageProtocol"));
        if p_bid != m_bid
            || (p_sk.is_empty() && m_sk != "full" && !m_sk.is_empty())
            || (!p_sk.is_empty() && p_sk != m_sk)
            || p_sp != OBJECT_SET_V1
        {
            return Err(AppError::invalid_payload(
                "Object-set control manifest does not match its receipt",
            ));
        }
    }

    let control_bytes: u64 = members
        .iter()
        .map(|m| m["control"]["expectedBytes"].as_u64().unwrap_or(0))
        .sum();
    let selection = normalize_selection(session.get("selection"))?;
    let projection = match selection {
        Some(ref sel) => Some(plan_projection(
            sel,
            &packages,
            control_bytes,
            session.get("selectionDigest").and_then(Value::as_str),
        )?),
        None => None,
    };

    let whole_chain_bytes: u64 = members
        .iter()
        .flat_map(|m| m["objects"].as_array().into_iter().flatten())
        .map(|o| o["size"].as_u64().unwrap_or(0))
        .sum();
    let mut required_payload_bytes = 0_u64;
    let mut required_count = 0usize;
    let mut total_payload_count = 0usize;
    let mut every_fetched = true;

    for (idx, member) in members.iter_mut().enumerate() {
        let existing_req: HashMap<String, Value> = member
            .get("requiredComponents")
            .and_then(Value::as_array)
            .map(|arr| {
                arr.iter()
                    .filter_map(|item| {
                        let d = item.get("objectDigest").and_then(Value::as_str)?;
                        Some((d.to_string(), item.clone()))
                    })
                    .collect()
            })
            .unwrap_or_default();
        let meta_dir = base.join(format!("metadata-{idx}"));
        let comp_map = Value::Object(read_json_object(&meta_dir.join("component-map.json"))?);
        let payload_index = Value::Object(read_json_object(&meta_dir.join("payload-index.json"))?);
        let (raw_paths, raw_descriptors) =
            validate_control_component_inventory(member, &comp_map, &payload_index)?;
        total_payload_count += raw_descriptors.len();

        let needed_paths: BTreeSet<String> = match projection {
            None => raw_paths.keys().cloned().collect(),
            Some(ref proj) if idx == 0 => proj.needed_full_entries.clone(),
            Some(ref proj) => {
                let (packs, _blobs, standalone) =
                    deepseek_policy::backup_projection::layer_needed_payloads(
                        &packages,
                        &proj.produced_by_layer,
                        idx,
                    )?;
                packs.union(&standalone).cloned().collect()
            }
        };
        let mut required_ids = BTreeSet::new();
        for p in &needed_paths {
            let cid = raw_paths.get(p).ok_or_else(|| {
                AppError::invalid_payload(format!("Object-set payload path has no component: {p}"))
            })?;
            if !raw_descriptors.contains_key(cid) {
                return Err(AppError::invalid_payload(format!(
                    "Object-set payload path has no component: {p}"
                )));
            }
            required_ids.insert(cid.clone());
        }

        let remote_by_digest: HashMap<String, &Value> = member
            .get("objects")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(|o| Some((o.get("digest")?.as_str()?.to_string(), o)))
            .collect();
        let ctrl_digest = truthy_str(member.get("controlObjectDigest"));
        let member_bid = truthy_str(member.get("backupId"));

        let mut required = Vec::with_capacity(required_ids.len());
        for cid in required_ids {
            let descriptor = raw_descriptors.get(&cid).ok_or_else(|| {
                AppError::invalid_payload("Object-set payload descriptor is invalid")
            })?;
            let digest = truthy_str(descriptor.get("ciphertextDigest"));
            let size = descriptor
                .get("ciphertextSize")
                .and_then(Value::as_u64)
                .unwrap_or(0);
            let remote = remote_by_digest.get(&digest).ok_or_else(|| {
                AppError::invalid_payload("Object-set control references a foreign component")
            })?;
            if remote.get("size").and_then(Value::as_u64) != Some(size) || digest == ctrl_digest {
                return Err(AppError::invalid_payload(
                    "Object-set control references a foreign component",
                ));
            }
            let existing = existing_req.get(&digest);
            let default_cpath = base
                .join(format!("payload-{idx:04}-{cid}.age"))
                .to_string_lossy()
                .into_owned();
            let fetched = existing
                .and_then(|e| e.get("fetched"))
                .and_then(Value::as_bool)
                .unwrap_or(false);
            let item = json!({
                "backupId": member_bid,
                "componentId": cid,
                "objectDigest": digest,
                "expectedBytes": size,
                "downloadedBytes": existing.and_then(|e| e.get("downloadedBytes")).and_then(Value::as_u64).unwrap_or(0),
                "remoteETag": Value::Null,
                "remoteVersionId": Value::Null,
                "ciphertextPath": existing.and_then(|e| e.get("ciphertextPath")).and_then(Value::as_str).unwrap_or(&default_cpath),
                "fetched": fetched,
                "plaintextSize": descriptor.get("plaintextSize").and_then(Value::as_u64).unwrap_or(0),
                "plaintextSha256": truthy_str(descriptor.get("plaintextSha256")),
            });
            every_fetched = every_fetched && fetched;
            required_payload_bytes = required_payload_bytes.saturating_add(size);
            required.push(item);
        }
        required_count += required.len();
        if let Some(m_obj) = member.as_object_mut() {
            m_obj.insert("requiredComponents".into(), Value::Array(required));
        }
    }

    let required_bytes = control_bytes.saturating_add(required_payload_bytes);
    let saved = whole_chain_bytes.saturating_sub(required_bytes);
    let ratio = if whole_chain_bytes > 0 {
        ((saved as f64 / whole_chain_bytes as f64) * 10_000.0).round() / 10_000.0
    } else {
        0.0
    };
    let network_selective = required_count < total_payload_count;
    let report = if let Some(ref proj) = projection {
        let mut rep = proj.report.as_object().cloned().unwrap_or_default();
        rep.insert(
            "networkSelectivityReason".into(),
            Value::String("object-set-component-closure".into()),
        );
        rep.insert("selectiveFetchSupported".into(), json!(true));
        rep.insert("networkSelective".into(), json!(network_selective));
        rep.insert("wholeChainCiphertextBytes".into(), json!(whole_chain_bytes));
        rep.insert("requiredCiphertextBytes".into(), json!(required_bytes));
        rep.insert("networkBytesSaved".into(), json!(saved));
        rep.insert("networkSavingsRatio".into(), json!(ratio));
        rep.insert("requiredComponents".into(), json!(required_count));
        rep.insert("totalComponents".into(), json!(total_payload_count));
        if let Some(bytes_obj) = rep.get_mut("bytes").and_then(Value::as_object_mut) {
            bytes_obj.insert("ciphertextDownloadBytes".into(), json!(required_bytes));
        }
        Value::Object(rep)
    } else {
        let last_bytes: u64 = packages
            .last()
            .map(|p| p.files.iter().map(|f| f.size).sum())
            .unwrap_or(0);
        json!({
            "selectiveFetchSupported": true,
            "networkSelective": network_selective,
            "wholeChainCiphertextBytes": whole_chain_bytes,
            "requiredCiphertextBytes": required_bytes,
            "networkBytesSaved": saved,
            "networkSavingsRatio": ratio,
            "requiredComponents": required_count,
            "totalComponents": total_payload_count,
            "bytes": {
                "selectedLogicalBytes": last_bytes,
                "dependencyBytes": 0,
                "ciphertextDownloadBytes": required_bytes,
                "estimatedMaterializedBytes": last_bytes,
            },
        })
    };

    session.insert("chain".into(), Value::Array(members));
    session.insert("projectionPlan".into(), report.clone());
    session.insert(
        "phase".into(),
        Value::String(
            if every_fetched {
                "components-fetched"
            } else {
                "fetching-selected-components"
            }
            .into(),
        ),
    );
    let session_val = Value::Object(session.clone());
    write_verified_plan(&base, &session_val, projection.as_ref(), &report)?;
    session.insert("updatedAt".into(), Value::String(now_utc_iso()));
    write_atomic_remote_session(&sp, session, false, false, false)?;
    Ok((projection, report))
}

pub(crate) fn preview_restore_from_target(
    root: &Path,
    target_id: &str,
    backup_id: &str,
    selection_val: Option<&Value>,
    restore_id_opt: Option<&str>,
) -> Result<Value, AppError> {
    let selection = normalize_selection(selection_val)?
        .ok_or_else(|| conflict_err("Restore selection requires at least one contributor"))?;
    let created = create_restore_from_target(
        root,
        target_id,
        backup_id,
        Some(&selection.canonical_value()),
        restore_id_opt,
    )?;
    let restore_id = truthy_str(created.get("restoreId"));
    let result = fetch_restore_session(root, &restore_id, None)?;
    let phase = truthy_str(result.get("phase"));
    if !matches!(
        phase.as_str(),
        "fetched" | "chain-fetched" | "controls-fetched"
    ) {
        return Ok(json!({
            "restoreId": restore_id,
            "phase": phase,
            "selectionDigest": created.get("selectionDigest").cloned().unwrap_or(Value::Null),
            "downloadedBytes": result.get("downloadedBytes").and_then(Value::as_u64).unwrap_or(0),
            "expectedBytes": result.get("expectedBytes").and_then(Value::as_u64).unwrap_or(0),
            "requiresSecret": false,
        }));
    }
    if !has_secret(&restore_id) {
        return Ok(json!({
            "restoreId": restore_id,
            "phase": phase,
            "selectionDigest": created.get("selectionDigest").cloned().unwrap_or(Value::Null),
            "requiresSecret": true,
        }));
    }

    let (kind, secret) = consume_secret(&restore_id, None)?;
    let secret_str = secret.as_str()?.to_string();
    let res = (|| -> Result<Value, AppError> {
        let mut session = read_remote_restore_session(root, &restore_id)?
            .ok_or_else(|| AppError::not_found("Remote restore session not found"))?;
        if session.get("storageProtocol").and_then(Value::as_str) == Some(OBJECT_SET_V1) {
            let (_proj, report) =
                plan_object_set_projection(root, &mut session, &kind, &secret_str)?;
            return Ok(json!({
                "restoreId": restore_id,
                "phase": "preview-planned",
                "selection": selection.canonical_value(),
                "selectionDigest": report.get("selectionDigest").cloned().unwrap_or_else(|| created["selectionDigest"].clone()),
                "requiresSecret": false,
                "projection": report,
            }));
        }
        let sp = remote_session_path(root, &restore_id)?;
        let base = sp
            .parent()
            .ok_or_else(route_support::unhandled_error)?
            .to_path_buf();
        let members = restore_members(&session)?;
        let mut decrypted_paths = Vec::with_capacity(members.len());
        for (idx, member) in members.iter().enumerate() {
            let cipher = PathBuf::from(truthy_str(member.get("ciphertextPath")));
            let decrypted = base.join(format!("preview-decrypted-{idx}.dsibackup"));
            decrypt_by_kind(&cipher, &decrypted, &kind, &secret_str)?;
            decrypted_paths.push(decrypted);
        }
        let packages = build_chain_packages(&decrypted_paths, &base)?;
        let ciphertext_download: u64 = members
            .iter()
            .map(|m| m.get("expectedBytes").and_then(Value::as_u64).unwrap_or(0))
            .sum();
        let plan = plan_projection(&selection, &packages, ciphertext_download, None)?;
        session.insert("projectionPlan".into(), plan.report.clone());
        session.insert("phase".into(), Value::String("preview-planned".into()));
        session.insert("updatedAt".into(), Value::String(now_utc_iso()));
        write_atomic_remote_session(&sp, &mut session, false, false, false)?;
        Ok(json!({
            "restoreId": restore_id,
            "phase": "preview-planned",
            "selection": selection.canonical_value(),
            "selectionDigest": plan.selection_digest,
            "requiresSecret": false,
            "projection": plan.report,
        }))
    })();
    let _ = put_secret_bytes(&restore_id, &kind, secret.as_slice());
    res
}

pub(crate) fn preflight_restore_session(
    root: &Path,
    restore_id: &str,
) -> Result<Value, PreflightFailure> {
    validate_restore_id(restore_id)?;
    let Some(mut session) = read_remote_restore_session(root, restore_id)? else {
        return Err(PreflightFailure::Error(AppError::not_found(
            "Remote restore session not found",
        )));
    };
    if session.get("storageProtocol").and_then(Value::as_str) != Some(OBJECT_SET_V1) {
        return Err(PreflightFailure::Error(conflict_err(
            "Recovery preflight requires object-set-v1",
        )));
    }
    let sp = remote_session_path(root, restore_id)?;
    let base = sp
        .parent()
        .ok_or_else(route_support::unhandled_error)?
        .to_path_buf();

    let session_val = Value::Object(session.clone());
    let projection_report = if let Some((_, rep)) = load_verified_plan(&base, &session_val) {
        rep
    } else {
        if !has_secret(restore_id) {
            return Err(PreflightFailure::Error(conflict_err(
                "Recovery preflight requires a verified plan or available unlock secret",
            )));
        }
        let (kind, secret) = consume_secret(restore_id, None)?;
        let secret_str = secret.as_str()?.to_string();
        let res = plan_object_set_projection(root, &mut session, &kind, &secret_str);
        let _ = put_secret_bytes(restore_id, &kind, secret.as_slice());
        let (_proj, rep) = res?;
        session = read_remote_restore_session(root, restore_id)?.unwrap_or(session);
        rep
    };

    let target_id = truthy_str(session.get("targetId"));
    let mut target_kind = "unavailable".to_string();
    let mut target_root: Option<PathBuf> = None;
    let mut catalog: Option<Map<String, Value>> = None;
    if let Ok(troot) = resolve_target_root(&target_id) {
        target_kind = if target_id == "managed-local" {
            "managed-local".into()
        } else {
            registered_target(&target_id)
                .ok()
                .and_then(|t| t.get("kind").and_then(Value::as_str).map(str::to_string))
                .unwrap_or_else(|| "filesystem".into())
        };
        catalog = catalog_state(&troot).ok();
        target_root = Some(troot);
    }

    let cache_root = component_cache_dir(root);
    let members = restore_members(&session)?;
    let mut components = Vec::new();
    for m in &members {
        if let Some(reqs) = m.get("requiredComponents").and_then(Value::as_array) {
            components.extend(reqs.iter().cloned());
        }
    }

    let mut blockers = Vec::new();
    let bytes_obj = projection_report.get("bytes").and_then(Value::as_object);
    if bytes_obj.is_none() {
        blockers.push(json!({
            "code": "projection-plan-invalid",
            "message": "Verified Projection Plan byte estimates are invalid.",
        }));
    }

    let mut target_reachable = target_root.is_some();
    let receipt_present = if let Some(ref troot) = target_root {
        let backup_ids: Vec<String> = {
            let chain_ids: Vec<String> = members
                .iter()
                .map(|m| truthy_str(m.get("backupId")))
                .collect();
            if chain_ids.is_empty() {
                vec![truthy_str(session.get("backupId"))]
            } else {
                chain_ids
            }
        };
        backup_ids.iter().all(|bid| {
            !bid.is_empty()
                && receipt_path(troot, bid)
                    .map(|p| p.is_file())
                    .unwrap_or(false)
        })
    } else {
        target_reachable = false;
        false
    };
    if !target_reachable || !receipt_present {
        blockers.push(json!({
            "code": "target-unavailable",
            "message": "Backup target receipt is unavailable.",
        }));
    }

    let mut cache_hits = 0usize;
    let mut cache_hit_bytes = 0_u64;
    let mut local_components = 0usize;
    let mut remote_bytes = 0_u64;
    let mut available_remote = 0usize;
    let mut missing_remote = 0usize;
    let mut plaintext_sizes = Vec::with_capacity(components.len());

    for comp in &components {
        let digest = truthy_str(comp.get("objectDigest"));
        let expected = comp
            .get("expectedBytes")
            .and_then(Value::as_u64)
            .unwrap_or(0);
        let p_size = comp
            .get("plaintextSize")
            .and_then(Value::as_u64)
            .unwrap_or(0);
        plaintext_sizes.push(p_size);
        if inspect_cached_component(&cache_root, &digest, expected) {
            cache_hits += 1;
            cache_hit_bytes = cache_hit_bytes.saturating_add(expected);
            local_components += 1;
            continue;
        }
        let local_path = PathBuf::from(truthy_str(comp.get("ciphertextPath")));
        if local_path.is_file()
            && fs::metadata(&local_path).map(|m| m.len()).ok() == Some(expected)
            && sha256_file(&local_path).ok().as_deref() == Some(digest.as_str())
        {
            local_components += 1;
            continue;
        }
        remote_bytes = remote_bytes.saturating_add(expected);
        let valid_remote = target_root.as_ref().is_some_and(|troot| {
            object_path(troot, &digest)
                .ok()
                .and_then(|p| fs::metadata(p).ok())
                .is_some_and(|m| m.len() == expected)
        });
        if valid_remote {
            available_remote += 1;
        } else {
            missing_remote += 1;
        }
    }
    if missing_remote > 0 {
        blockers.push(json!({
            "code": "required-component-unavailable",
            "message": "One or more required Components are unavailable.",
        }));
    }

    let live_logical = estimate_local_safety_logical_bytes(root);
    let mut safety = estimate_safety_backup_peak(live_logical);
    let ext_configured = !std::env::var("STATELESS_MCP_BACKUP_URL")
        .unwrap_or_default()
        .trim()
        .is_empty();
    safety.insert("externalBytesKnown".into(), json!(!ext_configured));
    if ext_configured {
        blockers.push(json!({
            "code": "external-safety-backup-size-unavailable",
            "message": "External safety backup size is unavailable.",
        }));
    }

    let free_disk_bytes = filesystem_disk_usage(root).map(|(_, _, avail)| avail);
    let materialized_est = bytes_obj
        .and_then(|b| b.get("estimatedMaterializedBytes"))
        .and_then(Value::as_u64)
        .unwrap_or(0);
    let safety_peak = safety
        .get("estimatedPeakBytes")
        .and_then(Value::as_u64)
        .unwrap_or(0);
    let cap = capacity_report(
        materialized_est,
        safety_peak,
        remote_bytes,
        &plaintext_sizes,
        free_disk_bytes,
        DEFAULT_DISK_RESERVE_BYTES,
    );
    if free_disk_bytes.is_none() {
        blockers.push(json!({
            "code": "disk-probe-failed",
            "message": "Recovery disk capacity could not be inspected.",
        }));
    } else if cap["disk"]["sufficient"].as_bool() != Some(true) {
        blockers.push(json!({
            "code": "insufficient-disk",
            "message": "Recovery disk capacity is insufficient.",
        }));
    }

    let full_health = whole_snapshot_health(&members, catalog.as_ref());
    let req_total = components.len();
    let total_components = projection_report
        .get("totalComponents")
        .and_then(Value::as_u64)
        .unwrap_or(req_total as u64)
        .max(req_total as u64);
    let ready = blockers.is_empty();
    let report = json!({
        "schemaVersion": 1,
        "restoreId": restore_id,
        "phase": "preflighted",
        "ready": ready,
        "closure": {
            "chainLength": members.len(),
            "selectedLogicalBytes": bytes_obj.and_then(|b| b.get("selectedLogicalBytes")).and_then(Value::as_u64).unwrap_or(0),
            "requiredComponents": req_total,
            "totalComponents": total_components,
            "localComponents": local_components,
        },
        "cache": {
            "hitComponents": cache_hits,
            "missComponents": req_total.saturating_sub(cache_hits),
            "hitBytes": cache_hit_bytes,
        },
        "network": {"remoteBytes": remote_bytes},
        "scratch": cap["scratch"],
        "disk": cap["disk"],
        "safetyBackup": safety,
        "targetHealth": {
            "status": if target_reachable && receipt_present { "ok" } else { "blocked" },
            "kind": target_kind,
            "receiptPresent": receipt_present,
        },
        "projectionRecoverability": {
            "status": if missing_remote == 0 { "recoverable" } else { "blocked" },
            "requiredComponents": req_total,
            "availableComponents": local_components + available_remote,
            "missingComponents": missing_remote,
        },
        "lastWholeSnapshotHealth": full_health,
        "blockingReasons": blockers,
    });

    session.insert("preflight".into(), report.clone());
    session.insert("preflightedAt".into(), Value::String(now_utc_iso()));
    session.insert("updatedAt".into(), Value::String(now_utc_iso()));
    write_atomic_remote_session(&sp, &mut session, false, false, false)?;

    if !ready {
        let cap_blocked = blockers.iter().any(|b| {
            matches!(
                b.get("code").and_then(Value::as_str),
                Some("insufficient-disk" | "disk-probe-failed")
            )
        });
        return Err(PreflightFailure::Blocked {
            code: if cap_blocked {
                RECOVERY_PREFLIGHT_CAPACITY_CODE
            } else {
                codes::INVALID_REQUEST
            },
            report,
        });
    }
    Ok(report)
}

fn whole_snapshot_health(members: &[Value], catalog: Option<&Map<String, Value>>) -> Value {
    let Some(cat) = catalog else {
        return json!({"status": "unavailable", "source": "catalog"});
    };
    let full_bid = members
        .iter()
        .rev()
        .find(|m| {
            m.get("snapshotKind")
                .and_then(Value::as_str)
                .unwrap_or("full")
                == "full"
        })
        .and_then(|m| m.get("backupId").and_then(Value::as_str));
    let Some(bid) = full_bid else {
        return json!({"status": "unavailable", "source": "catalog"});
    };
    let Some(record) = cat.get(bid).and_then(Value::as_object) else {
        return json!({"status": "unavailable", "source": "catalog"});
    };
    let scrub_ok = record.get("scrubOk").and_then(Value::as_bool);
    let unlock_at = record
        .get("userUnlockVerifiedAt")
        .cloned()
        .unwrap_or(Value::Null);
    let unlock_set = python_truthy(&unlock_at);
    let status = match (scrub_ok, unlock_set) {
        (Some(true), true) => "ok",
        (Some(false), _) => "error",
        _ => "warning",
    };
    json!({
        "status": status,
        "source": "catalog-scrub",
        "ciphertextScrubbedAt": record.get("ciphertextScrubbedAt").cloned().unwrap_or(Value::Null),
        "userUnlockVerifiedAt": unlock_at,
    })
}

fn write_checksums(tree_root: &Path, manifest: &Value) -> Result<(), AppError> {
    let manifest_bytes = dumps_compact(manifest).into_bytes();
    fs::write(tree_root.join("manifest.json"), &manifest_bytes)
        .map_err(|_| route_support::unhandled_error())?;
    let mut lines = Vec::new();
    if let Some(files) = manifest.get("files").and_then(Value::as_array) {
        for item in files {
            let sha = truthy_str(item.get("sha256"));
            let path = truthy_str(item.get("path"));
            lines.push(format!("{sha}  {path}"));
        }
    }
    lines.push(format!("{}  manifest.json", sha256_bytes(&manifest_bytes)));
    let body = format!("{}\n", lines.join("\n"));
    fs::write(tree_root.join("checksums.sha256"), body)
        .map_err(|_| route_support::unhandled_error())?;
    Ok(())
}

fn normalized_full_manifest(manifest: &Value) -> Value {
    let mut obj = manifest.as_object().cloned().unwrap_or_default();
    obj.insert("snapshotKind".into(), Value::String("full".into()));
    obj.remove("deltaFiles");
    Value::Object(obj)
}

fn selective_extract_members(
    decrypted_paths: &[PathBuf],
    packages: &[ChainPackage],
    base: &Path,
    projection: &ProjectionPlan,
) -> Result<Vec<PathBuf>, AppError> {
    let mut extracted_dirs = Vec::with_capacity(decrypted_paths.len());
    for (idx, decrypted) in decrypted_paths.iter().enumerate() {
        let extracted = base.join(format!("projected-{idx}"));
        let empty = BTreeSet::new();
        if idx == 0 {
            extract_selected_archive(
                decrypted,
                &extracted,
                &SelectedArchiveFilter {
                    needed_full: &projection.needed_full_entries,
                    needed_packs: &empty,
                    needed_standalone: &empty,
                },
                &packages[idx].manifest,
            )?;
        } else {
            let (packs, _blobs, standalone) =
                deepseek_policy::backup_projection::layer_needed_payloads(
                    packages,
                    &projection.produced_by_layer,
                    idx,
                )?;
            extract_selected_archive(
                decrypted,
                &extracted,
                &SelectedArchiveFilter {
                    needed_full: &empty,
                    needed_packs: &packs,
                    needed_standalone: &standalone,
                },
                &packages[idx].manifest,
            )?;
        }
        extracted_dirs.push(extracted);
    }
    Ok(extracted_dirs)
}

fn materialize_object_set_session(
    root: &Path,
    session: &mut Map<String, Value>,
    kind: &str,
    secret_str: &str,
) -> Result<Value, AppError> {
    let restore_id = truthy_str(session.get("restoreId"));
    let sp = remote_session_path(root, &restore_id)?;
    let base = sp
        .parent()
        .ok_or_else(route_support::unhandled_error)?
        .to_path_buf();
    let crypto_started = Instant::now();
    let session_val = Value::Object(session.clone());
    let (projection, projection_report) = match load_verified_plan(&base, &session_val) {
        Some(loaded) => loaded,
        None => {
            let planned = plan_object_set_projection(root, session, kind, secret_str)?;
            *session =
                read_remote_restore_session(root, &restore_id)?.unwrap_or_else(|| session.clone());
            planned
        }
    };

    let members = restore_members(session)?;
    for (idx, member) in members.iter().enumerate() {
        let metadata = base.join(format!("metadata-{idx}"));
        if metadata.is_dir() {
            continue;
        }
        let ctrl_cipher = PathBuf::from(truthy_str(
            member.get("control").and_then(|c| c.get("ciphertextPath")),
        ));
        let ctrl_dec = base.join(format!("control-decrypted-{idx}.zip"));
        decrypt_by_kind(&ctrl_cipher, &ctrl_dec, kind, secret_str)?;
        let _ = extract_archive_metadata(&ctrl_dec, &metadata)?;
    }

    if truthy_str(session.get("phase")) != "components-fetched" {
        let fetched = fetch_restore_session(root, &restore_id, None)?;
        let fphase = truthy_str(fetched.get("phase"));
        if fphase != "components-fetched" {
            return Err(conflict_err(OBJECT_SET_NOT_FETCHED_ERR));
        }
        *session =
            read_remote_restore_session(root, &restore_id)?.unwrap_or_else(|| session.clone());
    }

    session.insert(
        "phase".into(),
        Value::String("decrypting-components".into()),
    );
    session.insert("updatedAt".into(), Value::String(now_utc_iso()));
    write_atomic_remote_session(&sp, session, false, false, false)?;

    let members = restore_members(session)?;
    let mut extracted_dirs = Vec::with_capacity(members.len());
    let mut crypto_bytes = 0_u64;
    let mut crypto_components = 0usize;

    for (idx, member) in members.iter().enumerate() {
        let layer = base.join(format!("object-layer-{idx}"));
        if layer.exists() {
            let _ = fs::remove_dir_all(&layer);
        }
        let metadata = base.join(format!("metadata-{idx}"));
        copy_dir_tree(&metadata, &layer)?;
        let comp_map = read_json_object(&layer.join("component-map.json"))?;
        let components_obj = comp_map
            .get("components")
            .and_then(Value::as_object)
            .ok_or_else(|| AppError::invalid_payload("Object-set component map is invalid"))?;
        crypto_components += 1;
        crypto_bytes =
            crypto_bytes.saturating_add(member["control"]["expectedBytes"].as_u64().unwrap_or(0));

        if let Some(reqs) = member.get("requiredComponents").and_then(Value::as_array) {
            for req in reqs {
                crypto_components += 1;
                crypto_bytes = crypto_bytes.saturating_add(
                    req.get("expectedBytes")
                        .and_then(Value::as_u64)
                        .unwrap_or(0),
                );
                let cid = truthy_str(req.get("componentId"));
                let digest = truthy_str(req.get("objectDigest"));
                let expected_paths: Vec<String> = components_obj
                    .get(&cid)
                    .and_then(Value::as_array)
                    .ok_or_else(|| {
                        AppError::invalid_payload("Object-set component path map is invalid")
                    })?
                    .iter()
                    .filter_map(|v| v.as_str().map(str::to_string))
                    .collect();
                let safe_token = if digest.len() == 64 {
                    digest
                } else {
                    sha256_bytes(cid.as_bytes())
                };
                let dec_path = base.join(format!("payload-decrypted-{safe_token}.zip"));
                let cipher = PathBuf::from(truthy_str(req.get("ciphertextPath")));
                let step = (|| -> Result<(), AppError> {
                    decrypt_by_kind(&cipher, &dec_path, kind, secret_str)?;
                    let p_size = req
                        .get("plaintextSize")
                        .and_then(Value::as_u64)
                        .unwrap_or(u64::MAX);
                    let p_sha = truthy_str(req.get("plaintextSha256"));
                    let ok = dec_path.is_file()
                        && fs::metadata(&dec_path).map(|m| m.len()).ok() == Some(p_size)
                        && sha256_file(&dec_path).ok().as_deref() == Some(p_sha.as_str());
                    if !ok {
                        return Err(AppError::invalid_payload(
                            "Object-set component plaintext commitment mismatch",
                        ));
                    }
                    deepseek_policy::backup_unlock::extract_component_archive(
                        &dec_path,
                        &layer,
                        &expected_paths,
                    )?;
                    Ok(())
                })();
                scrub_plaintext_file(&dec_path);
                step?;
            }
        }
        extracted_dirs.push(layer);
    }

    record_telemetry_stage(
        session,
        "crypto",
        "success",
        crypto_started.elapsed().as_millis(),
        crypto_bytes,
        crypto_components,
    );
    session.insert("phase".into(), Value::String("materializing".into()));
    session.insert("updatedAt".into(), Value::String(now_utc_iso()));
    write_atomic_remote_session(&sp, session, false, false, false)?;

    let mat_started = Instant::now();
    let extracted = base.join("extracted");
    if extracted.exists() {
        let _ = fs::remove_dir_all(&extracted);
    }
    let mut final_manifest = materialize_chain(&extracted_dirs, &extracted, projection.as_ref())?;
    let skind = {
        let s = truthy_str(session.get("snapshotKind"));
        if s.is_empty() { "full".into() } else { s }
    };
    if projection.is_none() {
        final_manifest = normalized_full_manifest(&final_manifest);
        write_checksums(&extracted, &final_manifest)?;
        let _ = verify_manifest_tree(&extracted)?;
    }
    let (mat_bytes, mat_files) = manifest_work(&final_manifest);
    record_telemetry_stage(
        session,
        "materialization",
        "success",
        mat_started.elapsed().as_millis(),
        mat_bytes,
        mat_files,
    );
    session.insert("phase".into(), Value::String("verified".into()));
    session.insert("updatedAt".into(), Value::String(now_utc_iso()));
    write_atomic_remote_session(&sp, session, false, false, false)?;

    let mut result = Map::new();
    result.insert("restoreId".into(), Value::String(restore_id));
    result.insert("phase".into(), Value::String("materialized".into()));
    result.insert(
        "storageProtocol".into(),
        Value::String(OBJECT_SET_V1.into()),
    );
    result.insert("snapshotKind".into(), Value::String(skind.clone()));
    result.insert(
        "tree".into(),
        Value::String(extracted.to_string_lossy().into_owned()),
    );
    result.insert("manifest".into(), final_manifest);
    if skind == "incremental" {
        let bids: Vec<Value> = members.iter().map(|m| m["backupId"].clone()).collect();
        result.insert("chain".into(), Value::Array(bids));
    }
    if projection.is_some() {
        result.insert("projection".into(), projection_report);
    }
    Ok(Value::Object(result))
}

fn manifest_work(manifest: &Value) -> (u64, usize) {
    let Some(files) = manifest.get("files").and_then(Value::as_array) else {
        return (0, 0);
    };
    let bytes: u64 = files
        .iter()
        .map(|f| f.get("size").and_then(Value::as_u64).unwrap_or(0))
        .sum();
    (bytes, files.len())
}

fn copy_dir_tree(src: &Path, dst: &Path) -> Result<(), AppError> {
    if !src.is_dir() {
        return Ok(());
    }
    fs::create_dir_all(dst).map_err(|_| route_support::unhandled_error())?;
    for entry in fs::read_dir(src).map_err(|_| route_support::unhandled_error())? {
        let entry = entry.map_err(|_| route_support::unhandled_error())?;
        let p = entry.path();
        let target = dst.join(entry.file_name());
        if p.is_dir() {
            copy_dir_tree(&p, &target)?;
        } else if p.is_file() {
            if let Some(parent) = target.parent() {
                fs::create_dir_all(parent).map_err(|_| route_support::unhandled_error())?;
            }
            fs::copy(&p, &target).map_err(|_| route_support::unhandled_error())?;
        }
    }
    Ok(())
}

pub(crate) fn materialize_restore_session(
    root: &Path,
    restore_id: &str,
    kind: &str,
    secret_str: &str,
    drill: bool,
) -> Result<Value, AppError> {
    validate_restore_id(restore_id)?;
    let mut session = read_remote_restore_session(root, restore_id)?
        .ok_or_else(|| AppError::not_found("Remote restore session not found"))?;
    let sp = remote_session_path(root, restore_id)?;
    let base = sp
        .parent()
        .ok_or_else(route_support::unhandled_error)?
        .to_path_buf();
    if !drill {
        assert_live_restore_allowed(&base, &session)?;
    }
    if session.get("storageProtocol").and_then(Value::as_str) == Some(OBJECT_SET_V1) {
        return materialize_object_set_session(root, &mut session, kind, secret_str);
    }

    let phase = truthy_str(session.get("phase"));
    if !matches!(
        phase.as_str(),
        "fetched"
            | "chain-fetched"
            | "controls-fetched"
            | "fetching-selected-components"
            | "components-fetched"
            | "decrypting-controls"
            | "planning-projection"
            | "decrypting-components"
            | "decrypting-chain"
            | "materializing"
            | "verified"
    ) {
        return Err(conflict_err(OBJECT_SET_NOT_FETCHED_ERR));
    }

    session.insert("phase".into(), Value::String("decrypting-chain".into()));
    session.insert("updatedAt".into(), Value::String(now_utc_iso()));
    write_atomic_remote_session(&sp, &mut session, false, false, false)?;

    let crypto_started = Instant::now();
    let members = restore_members(&session)?;
    let mut decrypted_paths = Vec::with_capacity(members.len());
    let mut total_expected = 0_u64;
    for (idx, member) in members.iter().enumerate() {
        let cipher = PathBuf::from(truthy_str(member.get("ciphertextPath")));
        if !cipher.is_file() {
            return Err(AppError::not_found(
                "Restore session ciphertext is unavailable",
            ));
        }
        total_expected = total_expected.saturating_add(
            member
                .get("expectedBytes")
                .and_then(Value::as_u64)
                .unwrap_or(0),
        );
        let decrypted = base.join(format!("decrypted-{idx}.dsibackup"));
        decrypt_by_kind(&cipher, &decrypted, kind, secret_str)?;
        decrypted_paths.push(decrypted);
    }

    let frozen_selection = normalize_selection(session.get("selection"))?;
    let mut projection: Option<ProjectionPlan> = None;
    let mut packages = Vec::new();
    if let Some(ref sel) = frozen_selection {
        packages = build_chain_packages(&decrypted_paths, &base)?;
        let frozen_digest = truthy_str(session.get("selectionDigest"));
        let computed = sel.digest();
        if !frozen_digest.is_empty() && frozen_digest != computed {
            return Err(conflict_err(
                "Restore selection does not match the frozen session selection",
            ));
        }
        projection = Some(plan_projection(
            sel,
            &packages,
            total_expected,
            Some(&computed),
        )?);
    }

    record_telemetry_stage(
        &mut session,
        "crypto",
        "success",
        crypto_started.elapsed().as_millis(),
        total_expected,
        members.len(),
    );
    session.insert("phase".into(), Value::String("materializing".into()));
    session.insert("updatedAt".into(), Value::String(now_utc_iso()));
    write_atomic_remote_session(&sp, &mut session, false, false, false)?;

    let mat_started = Instant::now();
    let extracted = base.join("extracted");
    if extracted.exists() {
        let _ = fs::remove_dir_all(&extracted);
    }
    let extracted_dirs = if let Some(ref proj) = projection {
        selective_extract_members(&decrypted_paths, &packages, &base, proj)?
    } else {
        let mut dirs = Vec::with_capacity(decrypted_paths.len());
        for (idx, dec) in decrypted_paths.iter().enumerate() {
            let member_ext = base.join(format!("extracted-{idx}"));
            let _ = safe_extract_and_verify(dec, &member_ext)?;
            dirs.push(member_ext);
        }
        dirs
    };
    let mut final_manifest = materialize_chain(&extracted_dirs, &extracted, projection.as_ref())?;
    let skind = {
        let s = truthy_str(session.get("snapshotKind"));
        if s.is_empty() { "full".into() } else { s }
    };
    if projection.is_none() {
        final_manifest = normalized_full_manifest(&final_manifest);
        write_checksums(&extracted, &final_manifest)?;
        let _ = verify_manifest_tree(&extracted)?;
    }
    let (mat_bytes, mat_files) = manifest_work(&final_manifest);
    record_telemetry_stage(
        &mut session,
        "materialization",
        "success",
        mat_started.elapsed().as_millis(),
        mat_bytes,
        mat_files,
    );
    session.insert("phase".into(), Value::String("verified".into()));
    session.insert("updatedAt".into(), Value::String(now_utc_iso()));
    write_atomic_remote_session(&sp, &mut session, false, false, false)?;

    let mut result = Map::new();
    result.insert("restoreId".into(), Value::String(restore_id.into()));
    result.insert("phase".into(), Value::String("materialized".into()));
    result.insert("snapshotKind".into(), Value::String(skind.clone()));
    result.insert(
        "tree".into(),
        Value::String(extracted.to_string_lossy().into_owned()),
    );
    result.insert("manifest".into(), final_manifest);
    if skind == "incremental" {
        let bids: Vec<Value> = members.iter().map(|m| m["backupId"].clone()).collect();
        result.insert("chain".into(), Value::Array(bids));
    }
    if let Some(proj) = projection {
        result.insert("projection".into(), proj.report);
    }
    Ok(Value::Object(result))
}

fn assert_live_restore_allowed(
    session_dir: &Path,
    session: &Map<String, Value>,
) -> Result<(), AppError> {
    if session.get("drillOnly").is_some_and(python_truthy)
        || session_dir.join("drill-running.json").is_file()
        || session_dir.join("drill-result.json").is_file()
    {
        return Err(conflict_err("Recovery Drill job cannot enter live restore"));
    }
    Ok(())
}

fn materialize_federated_restore(
    root: &Path,
    restore_id: &str,
    payload: &Map<String, Value>,
) -> Result<Value, AppError> {
    validate_restore_id(restore_id)?;
    let mut session = read_remote_restore_session(root, restore_id)?
        .ok_or_else(|| AppError::not_found("Remote restore session not found"))?;
    let sp = remote_session_path(root, restore_id)?;
    let session_dir = sp
        .parent()
        .ok_or_else(route_support::unhandled_error)?
        .to_path_buf();
    assert_live_restore_allowed(&session_dir, &session)?;
    let controlled = converge_job_control(root, restore_id, &session_dir, &sp, &mut session)?;
    if matches!(
        controlled.as_str(),
        "paused" | "aborted" | "rolled-back" | "recovery-required"
    ) {
        return Ok(json!({
            "restoreId": restore_id,
            "phase": controlled,
            "remoteRestorePhase": controlled,
        }));
    }
    let phase = truthy_str(session.get("phase"));
    if matches!(
        phase.as_str(),
        "preparing" | "prepared" | "committing" | "complete"
    ) {
        let mut out = get_restore(root, restore_id)?
            .as_object()
            .cloned()
            .unwrap_or_default();
        out.insert("phase".into(), Value::String(phase.clone()));
        out.insert("remoteRestorePhase".into(), Value::String(phase));
        out.insert("materializedTreeVerified".into(), json!(true));
        return Ok(Value::Object(out));
    }
    if !matches!(
        phase.as_str(),
        "fetched"
            | "chain-fetched"
            | "controls-fetched"
            | "fetching-selected-components"
            | "components-fetched"
            | "decrypting-controls"
            | "planning-projection"
            | "decrypting-components"
            | "decrypting-chain"
            | "materializing"
            | "verified"
    ) {
        return Err(conflict_err(OBJECT_SET_NOT_FETCHED_ERR));
    }
    if session.get("storageProtocol").and_then(Value::as_str) == Some(OBJECT_SET_V1) {
        match preflight_restore_session(root, restore_id) {
            Ok(_) => {}
            Err(PreflightFailure::Error(err)) => return Err(err),
            Err(PreflightFailure::Blocked { .. }) => {
                return Err(conflict_err("Recovery preflight is blocked"));
            }
        }
        session = read_remote_restore_session(root, restore_id)?.unwrap_or(session);
    }

    let (kind, secret) = consume_secret(restore_id, None)?;
    let secret_str = secret.as_str()?.to_string();
    let res = (|| -> Result<Value, AppError> {
        let materialized =
            materialize_restore_session(root, restore_id, &kind, &secret_str, false)?;
        let current =
            read_remote_restore_session(root, restore_id)?.unwrap_or_else(|| session.clone());
        let extracted = PathBuf::from(truthy_str(materialized.get("tree")));
        let members = current
            .get("chain")
            .and_then(Value::as_array)
            .map(Vec::as_slice)
            .unwrap_or(&[]);
        let is_obj_set =
            current.get("storageProtocol").and_then(Value::as_str) == Some(OBJECT_SET_V1);
        let cipher_digest = if is_obj_set {
            members
                .last()
                .and_then(|m| m.get("objectSetDigest"))
                .or_else(|| current.get("objectSetDigest"))
                .and_then(Value::as_str)
                .map(str::to_string)
        } else {
            members
                .last()
                .and_then(|m| m.get("objectDigest"))
                .or_else(|| current.get("objectDigest"))
                .and_then(Value::as_str)
                .map(str::to_string)
        };
        let protection = if kind == "passphrase" {
            "passphrase"
        } else {
            "age-recipient"
        };
        let manifest = materialized
            .get("manifest")
            .cloned()
            .unwrap_or_else(|| json!({}));
        let archive_sha = tree_digest(&extracted)?;
        let meta = InspectMeta {
            restore_id,
            archive_sha256: archive_sha,
            encrypted: true,
            ciphertext_sha256: cipher_digest,
            protection,
        };
        if let Some(proj_rep) = materialized.get("projection").filter(|v| v.is_object()) {
            let _ = inspect_projected_restore_tree(
                root,
                manifest,
                &extracted,
                &session_dir,
                proj_rep,
                meta,
            )?;
        } else {
            let _ = inspect_verified_tree(root, manifest, &extracted, &session_dir, meta)?;
        }
        put_secret_bytes(restore_id, &kind, secret.as_slice())?;
        advance_federated_phase(root, restore_id, "preparing")?;
        let prepared = prepare_restore(root, restore_id, payload)?;
        let mut s_mut = read_remote_restore_session(root, restore_id)?.unwrap_or(current);
        let fed_phase = prepared
            .get("phase")
            .and_then(Value::as_str)
            .unwrap_or("backend-staged")
            .to_string();
        s_mut.insert("federatedPhase".into(), Value::String(fed_phase));
        s_mut.insert("phase".into(), Value::String("prepared".into()));
        s_mut.insert("updatedAt".into(), Value::String(now_utc_iso()));
        write_atomic_remote_session(&sp, &mut s_mut, false, false, false)?;

        let mut out = prepared.as_object().cloned().unwrap_or_default();
        out.insert("phase".into(), Value::String("prepared".into()));
        out.insert(
            "remoteRestorePhase".into(),
            Value::String("prepared".into()),
        );
        out.insert("materializedTreeVerified".into(), json!(true));
        Ok(Value::Object(out))
    })();

    if let Err(ref err) = res {
        let mut cur = read_remote_restore_session(root, restore_id)?.unwrap_or(session);
        if err.code == codes::INVALID_PAYLOAD {
            increment_telemetry_counter(&mut cur, "integrityFailure", 1);
        }
        let tx_exists = session_dir.join("transaction.json").is_file();
        let next_phase = if tx_exists {
            "recovery-required"
        } else {
            "failed"
        };
        cur.insert("phase".into(), Value::String(next_phase.into()));
        cur.insert("updatedAt".into(), Value::String(now_utc_iso()));
        let _ = write_atomic_remote_session(&sp, &mut cur, false, false, false);
        if !tx_exists {
            release_session_holds(root, &cur);
        }
        record_unlock_failure(restore_id);
    }
    res
}

fn restore_from_target(root: &Path, target_id: &str, backup_id: &str) -> Result<Value, AppError> {
    let created = create_restore_from_target(root, target_id, backup_id, None, None)?;
    let restore_id = truthy_str(created.get("restoreId"));
    let result = fetch_restore_session(root, &restore_id, None)?;
    let session = read_remote_restore_session(root, &restore_id)?;
    let first_path = session
        .as_ref()
        .and_then(|s| s.get("chain"))
        .and_then(Value::as_array)
        .and_then(|arr| arr.first())
        .and_then(|m| m.get("ciphertextPath"))
        .and_then(Value::as_str)
        .or_else(|| result.get("path").and_then(Value::as_str))
        .unwrap_or("package.age");
    let fname = Path::new(first_path)
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("package.age")
        .to_string();
    let phase = truthy_str(result.get("phase"));
    let mut out = result.as_object().cloned().unwrap_or_default();
    out.insert("targetId".into(), Value::String(target_id.into()));
    out.insert("backupId".into(), Value::String(backup_id.into()));
    out.insert("filename".into(), Value::String(fname));
    out.insert(
        "size".into(),
        json!(
            result
                .get("downloadedBytes")
                .and_then(Value::as_u64)
                .unwrap_or(0)
        ),
    );
    out.insert("requiresSecret".into(), json!(phase == "controls-fetched"));
    out.insert("hold".into(), json!({"restoreId": restore_id}));
    Ok(Value::Object(out))
}
