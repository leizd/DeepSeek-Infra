//! Native S5 restore transaction and session-control routes under `restore_staging_store`:
//! - `POST /api/workspace/restores/{restore_id}/prepare` (`prepare_restore`)
//! - `PUT  /api/workspace/restores/{restore_id}/frontend-prepared` (`frontend_prepared`)
//! - `POST /api/workspace/restores/{restore_id}/commit` (`commit_restore`)
//! - `POST /api/workspace/restores/{restore_id}/complete` (`complete_restore`)
//! - `POST /api/workspace/restores/{restore_id}/abort` (`abort_restore` / `request_restore_abort`)
//! - `POST /api/workspace/restores/{restore_id}/pause` (`request_restore_pause`)
//! - `POST /api/workspace/restores/{restore_id}/resume` (`resume_restore_session`)
//! - `POST /api/workspace/restores/{restore_id}/apply` (`apply_restore`)
//!
//! Write authority follows `DECLARED_NATIVE_DATA_DOMAINS`: all eight routes
//! require `DEEPSEEK_RUNTIME_MODE=python_disabled` (`restore_staging_store`) and
//! fail closed with `409 NATIVE_RESTORE_STAGING_STORE_WRITE_NOT_OWNED` otherwise.

use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::path::{Component, Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use axum::Router;
use axum::body::Bytes;
use axum::extract::Path as AxumPath;
use axum::http::{HeaderMap, Method};
use axum::response::Response;
use axum::routing::any;
use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::backup_mirror::{fsync_dir, sha256_file};
use deepseek_policy::backup_unlock::{
    derive_recipients, safe_extract_and_verify, scrub_plaintext_file, verify_manifest_tree,
};
use deepseek_policy::core_utils::python_truthy;
use deepseek_policy::entropy::{Entropy, SystemEntropy};
use deepseek_policy::file_lock::FileLockGuard;
use deepseek_policy::mutation_gate;
use deepseek_policy::python_json::{dumps_compact, value_str};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use crate::backup_archive_routes::{
    finalize_session_with_owner, gate_to_app_error, hex_digest, known_contributor, tree_digest,
    validate_staged_contributor,
};
use crate::backup_capacity_routes::now_utc_iso;
use crate::backup_catalog_routes::list_registered_targets;
use crate::backup_restore_read_routes::{public_restore, read_json_object, restore_root};
use crate::backup_restore_write_routes::{
    create_session, finish_json, require_write_authority, sha256_compact_json, write_atomic_json,
};
use crate::backup_secret_slots::{clear_secret, consume_secret, put_secret_bytes};
use crate::route_support;

const MAX_TX_BODY_BYTES: i64 = 2_000_000;
const FENCE_TTL_MS: i64 = 24 * 60 * 60 * 1000;
const UNBOUNDED_EXPIRY_MS: i64 = i64::MAX;
const REMOTE_SESSION_MAX_STAGE_SAMPLES: usize = 32;

struct ContributorFields {
    identity: &'static [&'static str],
    reference: &'static [&'static str],
    path: &'static [&'static str],
}

fn contributor_fields(id: &str) -> ContributorFields {
    match id {
        "projects" => ContributorFields {
            identity: &["id", "projectId"],
            reference: &[
                "projectId",
                "sourceProjectId",
                "parentProjectId",
                "mediaId",
                "artifactId",
                "savedId",
            ],
            path: &["path", "sourcePath", "artifactPath"],
        },
        "artifacts" => ContributorFields {
            identity: &["artifactId"],
            reference: &["artifactId", "projectId", "mediaId"],
            path: &["path", "sourcePath"],
        },
        "memory" => ContributorFields {
            identity: &["id"],
            reference: &["projectId", "sourceId"],
            path: &[],
        },
        "media" => ContributorFields {
            identity: &["mediaId"],
            reference: &["mediaId", "projectId", "sourceMediaId"],
            path: &["path", "sourcePath", "thumbnailPath"],
        },
        "custom-skills-and-packs" => ContributorFields {
            identity: &["skillId", "packId"],
            reference: &["skillId", "packId", "projectId"],
            path: &[],
        },
        "automations" => ContributorFields {
            identity: &["automationId"],
            reference: &["automationId", "projectId", "skillId", "packId"],
            path: &[],
        },
        "reminders" => ContributorFields {
            identity: &["id", "reminderId"],
            reference: &["projectId", "conversationId"],
            path: &[],
        },
        "agent-checkpoints" => ContributorFields {
            identity: &["runId"],
            reference: &["runId", "projectId", "conversationId"],
            path: &[],
        },
        "a2a-tasks" => ContributorFields {
            identity: &["taskId"],
            reference: &["taskId", "projectId"],
            path: &[],
        },
        "traces-and-run-history" => ContributorFields {
            identity: &["traceId", "runId"],
            reference: &["traceId", "runId", "projectId", "conversationId"],
            path: &[],
        },
        _ => ContributorFields {
            identity: &[],
            reference: &[],
            path: &[],
        },
    }
}

pub fn router() -> Router {
    Router::new()
        .route(
            "/api/workspace/restores/:restore_id/prepare",
            any(dispatch_prepare_restore),
        )
        .route(
            "/api/workspace/restores/:restore_id/frontend-prepared",
            any(dispatch_frontend_prepared),
        )
        .route(
            "/api/workspace/restores/:restore_id/commit",
            any(dispatch_commit_restore),
        )
        .route(
            "/api/workspace/restores/:restore_id/complete",
            any(dispatch_complete_restore),
        )
        .route(
            "/api/workspace/restores/:restore_id/abort",
            any(dispatch_abort_restore),
        )
        .route(
            "/api/workspace/restores/:restore_id/pause",
            any(dispatch_pause_restore),
        )
        .route(
            "/api/workspace/restores/:restore_id/resume",
            any(dispatch_resume_restore),
        )
        .route(
            "/api/workspace/restores/:restore_id/apply",
            any(dispatch_apply_restore),
        )
}

async fn dispatch_prepare_restore(
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
    let payload = match route_support::read_json_object(&headers, &body, MAX_TX_BODY_BYTES) {
        Ok(map) => map,
        Err(err) => return route_support::app_error_response(err),
    };
    let joined = tokio::task::spawn_blocking(move || {
        let root = crate::data_routes::workspace_root();
        prepare_restore(&root, &restore_id, &payload)
    })
    .await;
    finish_json(joined)
}

async fn dispatch_frontend_prepared(
    method: Method,
    AxumPath(restore_id): AxumPath<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if method != Method::PUT {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    if let Err(err) = require_write_authority() {
        return route_support::app_error_response(err);
    }
    let payload = match route_support::read_json_object(&headers, &body, MAX_TX_BODY_BYTES) {
        Ok(map) => map,
        Err(err) => return route_support::app_error_response(err),
    };
    let joined = tokio::task::spawn_blocking(move || {
        let root = crate::data_routes::workspace_root();
        let digest = payload
            .get("digest")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        frontend_prepared(&root, &restore_id, &digest)
    })
    .await;
    finish_json(joined)
}

async fn dispatch_commit_restore(
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
    let payload = match route_support::read_json_object(&headers, &body, MAX_TX_BODY_BYTES) {
        Ok(map) => map,
        Err(err) => return route_support::app_error_response(err),
    };
    let joined = tokio::task::spawn_blocking(move || {
        let root = crate::data_routes::workspace_root();
        let frontend_committed = payload.get("frontendCommitted").is_some_and(python_truthy);
        let frontend_digest = payload
            .get("frontendDigest")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .filter(|s| !s.is_empty());
        advance_federated_phase(&root, &restore_id, "committing")?;
        commit_restore(
            &root,
            &restore_id,
            frontend_committed,
            frontend_digest.as_deref(),
        )
    })
    .await;
    finish_json(joined)
}

async fn dispatch_complete_restore(
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
    let payload = match route_support::read_json_object(&headers, &body, MAX_TX_BODY_BYTES) {
        Ok(map) => map,
        Err(err) => return route_support::app_error_response(err),
    };
    let joined = tokio::task::spawn_blocking(move || {
        let root = crate::data_routes::workspace_root();
        let frontend_digest = payload
            .get("frontendDigest")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .filter(|s| !s.is_empty());
        let result = complete_restore(&root, &restore_id, frontend_digest.as_deref())?;
        advance_federated_phase(&root, &restore_id, "complete")?;
        Ok(result)
    })
    .await;
    finish_json(joined)
}

async fn dispatch_abort_restore(
    method: Method,
    AxumPath(restore_id): AxumPath<String>,
) -> Response {
    if method != Method::POST {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    if let Err(err) = require_write_authority() {
        return route_support::app_error_response(err);
    }
    let joined = tokio::task::spawn_blocking(move || {
        let root = crate::data_routes::workspace_root();
        if read_remote_restore_session(&root, &restore_id)?.is_some() {
            return request_restore_abort(&root, &restore_id);
        }
        let result = abort_restore(&root, &restore_id)?;
        advance_federated_phase(&root, &restore_id, "rolled-back")?;
        Ok(result)
    })
    .await;
    finish_json(joined)
}

async fn dispatch_pause_restore(
    method: Method,
    AxumPath(restore_id): AxumPath<String>,
) -> Response {
    if method != Method::POST {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    if let Err(err) = require_write_authority() {
        return route_support::app_error_response(err);
    }
    let joined = tokio::task::spawn_blocking(move || {
        let root = crate::data_routes::workspace_root();
        request_restore_pause(&root, &restore_id)
    })
    .await;
    finish_json(joined)
}

async fn dispatch_resume_restore(
    method: Method,
    AxumPath(restore_id): AxumPath<String>,
) -> Response {
    if method != Method::POST {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    if let Err(err) = require_write_authority() {
        return route_support::app_error_response(err);
    }
    let joined = tokio::task::spawn_blocking(move || {
        let root = crate::data_routes::workspace_root();
        resume_restore_session(&root, &restore_id)
    })
    .await;
    finish_json(joined)
}

async fn dispatch_apply_restore(
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
    let payload = match route_support::read_json_object(&headers, &body, MAX_TX_BODY_BYTES) {
        Ok(map) => map,
        Err(err) => return route_support::app_error_response(err),
    };
    let joined = tokio::task::spawn_blocking(move || {
        let root = crate::data_routes::workspace_root();
        let mode = payload
            .get("mode")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| "merge".to_string());
        apply_restore(&root, &restore_id, &mode)
    })
    .await;
    finish_json(joined)
}

fn now_unix_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| i64::try_from(d.as_millis()).unwrap_or(i64::MAX))
        .unwrap_or(0)
}

fn format_uuid_like() -> Result<String, AppError> {
    let hex = SystemEntropy.new_id()?;
    if hex.len() >= 32 {
        Ok(format!(
            "{}-{}-{}-{}-{}",
            &hex[0..8],
            &hex[8..12],
            &hex[12..16],
            &hex[16..20],
            &hex[20..32]
        ))
    } else {
        Ok(hex)
    }
}

fn safe_restore_session_id(restore_id: &str) -> Result<(), AppError> {
    if restore_id.is_empty()
        || restore_id == "."
        || restore_id == ".."
        || restore_id.contains('/')
        || restore_id.contains('\\')
        || restore_id.contains('\0')
        || restore_id.contains(':')
    {
        return Err(AppError::invalid_payload("Invalid restore id"));
    }
    let mut comps = Path::new(restore_id).components();
    match (comps.next(), comps.next()) {
        (Some(Component::Normal(_)), None) => Ok(()),
        _ => Err(AppError::invalid_payload("Invalid restore id")),
    }
}

fn safe_tree_digest(dir: &Path) -> Result<String, AppError> {
    if !dir.is_dir() {
        return Ok(hex_digest(&Sha256::digest(b"")));
    }
    tree_digest(dir)
}

fn remove_path_if_exists(path: &Path) -> Result<(), AppError> {
    match fs::symlink_metadata(path) {
        Ok(meta) => {
            if meta.is_dir() {
                fs::remove_dir_all(path).map_err(|_| route_support::unhandled_error())
            } else {
                fs::remove_file(path).map_err(|_| route_support::unhandled_error())
            }
        }
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(_) => Err(route_support::unhandled_error()),
    }
}

fn copy_tree_recursive(src: &Path, dst: &Path) -> Result<(), AppError> {
    fs::create_dir_all(dst).map_err(|_| route_support::unhandled_error())?;
    let mut entries = fs::read_dir(src)
        .map_err(|_| route_support::unhandled_error())?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|_| route_support::unhandled_error())?;
    entries.sort_by_key(|a| a.file_name());
    for entry in entries {
        let from = entry.path();
        let to = dst.join(entry.file_name());
        let meta = fs::symlink_metadata(&from).map_err(|_| route_support::unhandled_error())?;
        if meta.file_type().is_symlink() {
            return Err(AppError::invalid_payload("Backup contains a symlink"));
        }
        if meta.is_dir() {
            copy_tree_recursive(&from, &to)?;
        } else if meta.is_file() {
            fs::copy(&from, &to).map_err(|_| route_support::unhandled_error())?;
        }
    }
    Ok(())
}

fn fsync_file_best_effort(path: &Path) {
    if let Ok(file) = OpenOptions::new().read(true).write(true).open(path) {
        let _ = file.sync_all();
    }
}

fn fsync_tree(root: &Path) {
    if !root.is_dir() {
        return;
    }
    let mut files = Vec::new();
    let mut dirs = Vec::new();
    fn walk(dir: &Path, files: &mut Vec<PathBuf>, dirs: &mut Vec<PathBuf>) {
        if let Ok(entries) = fs::read_dir(dir) {
            for entry in entries.flatten() {
                let p = entry.path();
                if let Ok(meta) = fs::symlink_metadata(&p) {
                    if meta.is_dir() {
                        dirs.push(p.clone());
                        walk(&p, files, dirs);
                    } else if meta.is_file() {
                        files.push(p);
                    }
                }
            }
        }
    }
    walk(root, &mut files, &mut dirs);
    files.sort();
    for file in files {
        fsync_file_best_effort(&file);
    }
    dirs.sort_by_key(|p| std::cmp::Reverse(p.components().count()));
    for dir in dirs {
        fsync_dir(&dir);
    }
    fsync_dir(root);
}

fn is_valid_mode(mode: &str) -> bool {
    matches!(mode, "merge" | "project-copy" | "replace-empty")
}

fn is_64_hex_lowercase(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|b| b.is_ascii_hexdigit())
}

fn stem_and_suffix(part: &str) -> (&str, &str) {
    if part.is_empty() || part == "." || part == ".." {
        return (part, "");
    }
    if let Some(dot) = part.rfind('.') {
        if dot > 0 && dot + 1 < part.len() {
            return (&part[..dot], &part[dot..]);
        }
    }
    (part, "")
}

fn restored_id(original_id: &str, backup_id: &str, source_digest: &str) -> String {
    let prefix: String = original_id.chars().take(43).collect();
    let mut hasher = Sha256::new();
    hasher.update(format!("{backup_id}\0{original_id}\0{source_digest}").as_bytes());
    let digest = hex_digest(&hasher.finalize());
    format!("{prefix}-imported-{}", &digest[..10])
}

fn collect_sorted_json_files(dir: &Path, out: &mut Vec<(String, PathBuf)>, base: &Path) {
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let p = entry.path();
        if p.is_dir() {
            collect_sorted_json_files(&p, out, base);
        } else if p.is_file() {
            let is_json = p
                .extension()
                .and_then(|e| e.to_str())
                .is_some_and(|e| e.eq_ignore_ascii_case("json"));
            if is_json {
                let rel = p
                    .strip_prefix(base)
                    .unwrap_or(&p)
                    .to_string_lossy()
                    .replace('\\', "/");
                out.push((rel, p));
            }
        }
    }
}

fn json_identity_index(dir: &Path, identity_fields: &[&str]) -> BTreeMap<String, String> {
    let mut result = BTreeMap::new();
    if !dir.is_dir() || identity_fields.is_empty() {
        return result;
    }
    let mut files = Vec::new();
    collect_sorted_json_files(dir, &mut files, dir);
    files.sort_by(|a, b| a.0.cmp(&b.0));

    fn visit(value: &Value, fields: &[&str], out: &mut BTreeMap<String, String>) {
        match value {
            Value::Array(items) => {
                for item in items {
                    visit(item, fields, out);
                }
            }
            Value::Object(map) => {
                let encoded = sha256_compact_json(value);
                for key in fields {
                    if let Some(Value::String(cand)) = map.get(*key) {
                        if !cand.is_empty() {
                            out.entry(cand.clone()).or_insert_with(|| encoded.clone());
                        }
                    }
                }
                for child in map.values() {
                    visit(child, fields, out);
                }
            }
            _ => {}
        }
    }

    for (_, path) in files {
        let Ok(raw) = fs::read_to_string(&path) else {
            continue;
        };
        let Ok(parsed) = serde_json::from_str::<Value>(&raw) else {
            continue;
        };
        visit(&parsed, identity_fields, &mut result);
    }
    result
}

fn restore_identity_map(
    plan: &Map<String, Value>,
    restore_dir: &Path,
    mode: &str,
    projected: bool,
) -> Result<BTreeMap<String, String>, AppError> {
    if mode != "merge" && mode != "project-copy" {
        return Ok(BTreeMap::new());
    }
    let source_root = restore_dir.join(if projected { "extracted" } else { "verified" });
    let operations = plan
        .get("operations")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let backup_id = plan
        .get("manifest")
        .and_then(|m| m.get("backupId"))
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();

    let mut result = BTreeMap::new();
    let project_op = operations
        .iter()
        .find(|item| item.get("contributorId").and_then(Value::as_str) == Some("projects"));
    if let Some(op) = project_op {
        let op_src = resolve_operation_source(op, &source_root, "projects");
        let op_dst = PathBuf::from(
            op.get("destination")
                .and_then(Value::as_str)
                .unwrap_or_default(),
        );
        if op_src.is_dir() {
            let mut project_dirs = fs::read_dir(&op_src)
                .map_err(|_| route_support::unhandled_error())?
                .filter_map(|e| e.ok())
                .filter(|e| e.path().is_dir())
                .collect::<Vec<_>>();
            project_dirs.sort_by_key(|e| e.file_name());
            for entry in project_dirs {
                let name = entry.file_name().to_string_lossy().into_owned();
                let proj_path = entry.path();
                let existing = op_dst.join(&name);
                let src_digest = safe_tree_digest(&proj_path)?;
                let collide = mode == "project-copy"
                    || (existing.is_dir() && safe_tree_digest(&existing)? != src_digest);
                if collide {
                    result.insert(name.clone(), restored_id(&name, &backup_id, &src_digest));
                }
            }
        }
    }

    for op in &operations {
        let Some(cid) = op.get("contributorId").and_then(Value::as_str) else {
            continue;
        };
        if op.get("external").is_some_and(python_truthy) || known_contributor(cid).is_none() {
            continue;
        }
        let op_src = resolve_operation_source(op, &source_root, cid);
        let op_dst = PathBuf::from(
            op.get("destination")
                .and_then(Value::as_str)
                .unwrap_or_default(),
        );
        let fields = contributor_fields(cid);
        let src_idx = json_identity_index(&op_src, fields.identity);
        let dst_idx = json_identity_index(&op_dst, fields.identity);
        for (original_id, source_digest) in src_idx {
            if let Some(existing_digest) = dst_idx.get(&original_id) {
                if existing_digest != &source_digest && !result.contains_key(&original_id) {
                    result.insert(
                        original_id.clone(),
                        restored_id(&original_id, &backup_id, &source_digest),
                    );
                }
            }
        }
    }
    Ok(result)
}

fn resolve_operation_source(op: &Value, fallback_base: &Path, contributor_id: &str) -> PathBuf {
    let raw = op.get("source").and_then(Value::as_str).unwrap_or_default();
    if !raw.is_empty() {
        let p = PathBuf::from(raw);
        if p.exists() {
            return p;
        }
    }
    fallback_base.join("payload").join(contributor_id)
}

fn rewrite_typed_json_references(
    fields: &ContributorFields,
    raw: &[u8],
    identity_map: &BTreeMap<String, String>,
    backup_id: &str,
) -> Vec<u8> {
    let Ok(text) = std::str::from_utf8(raw) else {
        return raw.to_vec();
    };
    let Ok(parsed) = serde_json::from_str::<Value>(text) else {
        return raw.to_vec();
    };

    fn rewrite_path(item: &str, identity_map: &BTreeMap<String, String>) -> String {
        let normalized = item.replace('\\', "/");
        normalized
            .split('/')
            .map(|part| {
                if let Some(mapped) = identity_map.get(part) {
                    mapped.clone()
                } else {
                    let (stem, suffix) = stem_and_suffix(part);
                    let mapped_stem = identity_map.get(stem).map(String::as_str).unwrap_or(stem);
                    format!("{mapped_stem}{suffix}")
                }
            })
            .collect::<Vec<_>>()
            .join("/")
    }

    fn rewrite(
        item: &Value,
        fields: &ContributorFields,
        identity_map: &BTreeMap<String, String>,
        backup_id: &str,
    ) -> Value {
        match item {
            Value::Array(items) => Value::Array(
                items
                    .iter()
                    .map(|v| rewrite(v, fields, identity_map, backup_id))
                    .collect(),
            ),
            Value::Object(map) => {
                let mut out = Map::new();
                let mut original_id: Option<String> = None;
                for (key, child) in map {
                    let in_id = fields.identity.contains(&key.as_str());
                    let in_ref = fields.reference.contains(&key.as_str());
                    let in_path = fields.path.contains(&key.as_str());
                    if let Value::String(s) = child {
                        if in_id || in_ref {
                            if in_id && identity_map.contains_key(s) {
                                original_id = Some(s.clone());
                            }
                            let mapped = identity_map.get(s).cloned().unwrap_or_else(|| s.clone());
                            out.insert(key.clone(), Value::String(mapped));
                        } else if in_path {
                            out.insert(key.clone(), Value::String(rewrite_path(s, identity_map)));
                        } else {
                            out.insert(key.clone(), Value::String(s.clone()));
                        }
                    } else {
                        out.insert(key.clone(), rewrite(child, fields, identity_map, backup_id));
                    }
                }
                if let Some(orig) = original_id {
                    out.insert(
                        "importedFrom".into(),
                        json!({
                            "originalId": orig,
                            "sourceBackupId": backup_id,
                        }),
                    );
                }
                Value::Object(out)
            }
            _ => item.clone(),
        }
    }

    let rewritten = rewrite(&parsed, fields, identity_map, backup_id);
    dumps_compact(&rewritten).into_bytes()
}

fn merge_json_payload(existing_raw: &[u8], incoming_raw: &[u8]) -> Vec<u8> {
    let (Ok(ex_str), Ok(inc_str)) = (
        std::str::from_utf8(existing_raw),
        std::str::from_utf8(incoming_raw),
    ) else {
        return incoming_raw.to_vec();
    };
    let (Ok(existing), Ok(incoming)) = (
        serde_json::from_str::<Value>(ex_str),
        serde_json::from_str::<Value>(inc_str),
    ) else {
        return incoming_raw.to_vec();
    };

    fn merge(left: Value, right: Value) -> Value {
        match (left, right) {
            (Value::Array(left_arr), Value::Array(right_arr)) => {
                let mut seen: BTreeSet<String> = left_arr.iter().map(sha256_compact_json).collect();
                let mut result = left_arr;
                for item in right_arr {
                    let fp = sha256_compact_json(&item);
                    if seen.insert(fp) {
                        result.push(item);
                    }
                }
                Value::Array(result)
            }
            (Value::Object(mut left_map), Value::Object(right_map)) => {
                for (key, value) in right_map {
                    if let Some(existing_val) = left_map.remove(&key) {
                        left_map.insert(key, merge(existing_val, value));
                    } else {
                        left_map.insert(key, value);
                    }
                }
                Value::Object(left_map)
            }
            (Value::Null, right_val) => right_val,
            (left_val, _) => left_val,
        }
    }

    dumps_compact(&merge(existing, incoming)).into_bytes()
}

fn collision_path(target: &Path, project_ids: &[String]) -> PathBuf {
    let target_posix = target.to_string_lossy().replace('\\', "/");
    let seed_input = format!("{target_posix}|{}", project_ids.join(","));
    let seed = &hex_digest(&Sha256::digest(seed_input.as_bytes()))[..12];
    let file_name = target
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or("file");
    let (stem, suffix) = stem_and_suffix(file_name);
    target.with_file_name(format!("{stem}.imported-{seed}{suffix}"))
}

struct StagingMergePlan<'a> {
    contributor_id: &'a str,
    source_dir: &'a Path,
    staged_dir: &'a Path,
    mode: &'a str,
    identity_map: &'a BTreeMap<String, String>,
    source_backup_id: &'a str,
    context_project_ids: &'a [String],
}

fn apply_restore_into_staging(plan: &StagingMergePlan<'_>) -> Result<(), AppError> {
    if plan.mode == "replace-empty" && plan.staged_dir.exists() {
        let non_empty = fs::read_dir(plan.staged_dir)
            .map_err(|_| route_support::unhandled_error())?
            .next()
            .is_some();
        if non_empty {
            return Err(AppError {
                message: format!("{} is not empty", plan.contributor_id),
                code: codes::INVALID_REQUEST,
                status: 409,
            });
        }
    }
    fs::create_dir_all(plan.staged_dir).map_err(|_| route_support::unhandled_error())?;
    if !plan.source_dir.is_dir() {
        return Ok(());
    }

    let mut files = Vec::new();
    fn collect_sorted_files(
        base: &Path,
        dir: &Path,
        out: &mut Vec<(String, PathBuf)>,
    ) -> Result<(), AppError> {
        for entry in fs::read_dir(dir).map_err(|_| route_support::unhandled_error())? {
            let entry = entry.map_err(|_| route_support::unhandled_error())?;
            let path = entry.path();
            if path.is_dir() {
                collect_sorted_files(base, &path, out)?;
            } else if path.is_file() {
                let rel = path
                    .strip_prefix(base)
                    .map_err(|_| route_support::unhandled_error())?
                    .to_string_lossy()
                    .replace('\\', "/");
                out.push((rel, path));
            }
        }
        Ok(())
    }
    collect_sorted_files(plan.source_dir, plan.source_dir, &mut files)?;
    files.sort_by(|a, b| a.0.cmp(&b.0));

    let fields = contributor_fields(plan.contributor_id);
    for (rel, item) in files {
        let mut target_rel = PathBuf::new();
        for part in rel.split('/').filter(|s| !s.is_empty()) {
            if !plan.identity_map.is_empty() {
                if let Some(mapped) = plan.identity_map.get(part) {
                    target_rel.push(mapped);
                } else {
                    let (stem, suffix) = stem_and_suffix(part);
                    if let Some(mapped_stem) = plan.identity_map.get(stem) {
                        target_rel.push(format!("{mapped_stem}{suffix}"));
                    } else {
                        target_rel.push(part);
                    }
                }
            } else {
                target_rel.push(part);
            }
        }

        let mut target = plan.staged_dir.join(target_rel);
        if let Some(parent) = target.parent() {
            fs::create_dir_all(parent).map_err(|_| route_support::unhandled_error())?;
        }
        let is_json = item
            .extension()
            .and_then(|e| e.to_str())
            .is_some_and(|e| e.eq_ignore_ascii_case("json"));
        let rewritten = if is_json && !plan.identity_map.is_empty() {
            let raw = fs::read(&item).map_err(|_| route_support::unhandled_error())?;
            Some(rewrite_typed_json_references(
                &fields,
                &raw,
                plan.identity_map,
                plan.source_backup_id,
            ))
        } else {
            None
        };

        if target.exists() {
            let incoming_digest = if let Some(ref bytes) = rewritten {
                hex_digest(&Sha256::digest(bytes))
            } else {
                sha256_file(&item).map_err(|_| route_support::unhandled_error())?
            };
            let existing_digest =
                sha256_file(&target).map_err(|_| route_support::unhandled_error())?;
            if existing_digest == incoming_digest {
                continue;
            }
            if is_json && (plan.mode == "merge" || plan.mode == "project-copy") {
                let existing_bytes =
                    fs::read(&target).map_err(|_| route_support::unhandled_error())?;
                let incoming_bytes = match rewritten {
                    Some(bytes) => bytes,
                    None => fs::read(&item).map_err(|_| route_support::unhandled_error())?,
                };
                let merged = merge_json_payload(&existing_bytes, &incoming_bytes);
                fs::write(&target, merged).map_err(|_| route_support::unhandled_error())?;
                continue;
            }
            if plan.mode == "merge" || plan.mode == "project-copy" {
                target = collision_path(&target, plan.context_project_ids);
            }
        }

        if let Some(bytes) = rewritten {
            fs::write(&target, bytes).map_err(|_| route_support::unhandled_error())?;
        } else {
            fs::copy(&item, &target).map_err(|_| route_support::unhandled_error())?;
        }
    }
    Ok(())
}

fn verified_extracted_manifest(
    restore_dir: &Path,
    plan: &Map<String, Value>,
) -> Result<Value, AppError> {
    let extracted = restore_dir.join("extracted");
    let manifest_path = extracted.join("manifest.json");
    if !manifest_path.is_file() {
        return Err(AppError {
            message: "Decrypted backup payload is unavailable; re-provide the backup secret"
                .to_string(),
            code: codes::INVALID_REQUEST,
            status: 409,
        });
    }
    let expected_manifest_digest = plan
        .get("manifestDigest")
        .and_then(Value::as_str)
        .unwrap_or("");
    let actual_manifest_digest = sha256_file(&manifest_path).map_err(|_| {
        AppError::invalid_payload("Decrypted backup payload changed after inspection")
    })?;
    if actual_manifest_digest != expected_manifest_digest {
        return Err(AppError::invalid_payload(
            "Decrypted backup payload changed after inspection",
        ));
    }
    let manifest = verify_manifest_tree(&extracted)?;
    let expected_tree_digest = plan
        .get("extractedTreeDigest")
        .and_then(Value::as_str)
        .unwrap_or("");
    if tree_digest(&extracted)? != expected_tree_digest {
        return Err(AppError::invalid_payload(
            "Decrypted backup payload changed after inspection",
        ));
    }
    let verified = restore_dir.join("verified");
    remove_path_if_exists(&verified)?;
    copy_tree_recursive(&extracted, &verified)?;
    Ok(manifest)
}

fn create_safety_backup(
    root: &Path,
    plan: &Map<String, Value>,
    restore_id: &str,
) -> Result<Value, AppError> {
    let mut request = Map::new();
    request.insert("mode".into(), Value::String("full".to_string()));
    request.insert("includeHistory".into(), Value::Bool(true));
    request.insert("requiresFrontendState".into(), Value::Bool(false));
    request.insert(
        "coveragePolicy".into(),
        Value::String("best-effort".to_string()),
    );

    let mut held_secret: Option<(String, crate::backup_secret_slots::SecretBytes)> = None;
    if plan.get("encrypted").is_some_and(python_truthy) {
        let expected_kind = if plan.get("protection").and_then(Value::as_str) == Some("passphrase")
        {
            "passphrase"
        } else {
            "age-identity"
        };
        let (kind, secret) = consume_secret(restore_id, Some(expected_kind))?;
        if kind == "passphrase" {
            request.insert("protection".into(), json!({ "mode": "passphrase" }));
        } else {
            let recipients = derive_recipients(secret.as_str()?)?;
            request.insert(
                "protection".into(),
                json!({ "mode": "age-recipient", "recipients": recipients }),
            );
        }
        held_secret = Some((kind, secret));
    }

    let created = create_session(root, &request)?;
    let backup_id = created
        .get("backupId")
        .and_then(Value::as_str)
        .ok_or_else(route_support::unhandled_error)?
        .to_string();
    if let Some((ref kind, ref secret)) = held_secret {
        put_secret_bytes(&backup_id, kind, secret.as_slice())?;
    }
    finalize_session_with_owner(root, &backup_id, Some(restore_id))
}

fn stateless_mcp_request(
    path: &str,
    method: reqwest::Method,
    body: Option<Vec<u8>>,
    headers: &[(&str, &str)],
    timeout_secs: u64,
) -> Result<Value, AppError> {
    let base = std::env::var("STATELESS_MCP_BACKUP_URL")
        .unwrap_or_default()
        .trim()
        .trim_end_matches('/')
        .to_string();
    if base.is_empty() {
        return Err(AppError {
            message: "Stateless MCP backup endpoint is not configured".to_string(),
            code: codes::INVALID_REQUEST,
            status: 409,
        });
    }
    let client = reqwest::blocking::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(timeout_secs))
        .build()
        .map_err(|_| route_support::unhandled_error())?;
    let mut req = client
        .request(method, format!("{base}{path}"))
        .header("Accept", "application/json, application/x-ndjson");
    let token = std::env::var("STATELESS_MCP_BACKUP_TOKEN")
        .unwrap_or_default()
        .trim()
        .to_string();
    if !token.is_empty() {
        req = req.bearer_auth(token);
    }
    if let Some(bytes) = body {
        let content_type = if path.starts_with("/internal/restores/") {
            "application/x-ndjson"
        } else {
            "application/json"
        };
        req = req.header("Content-Type", content_type).body(bytes);
    }
    for (k, v) in headers {
        req = req.header(*k, *v);
    }
    let resp = req.send().map_err(|_| AppError {
        message: "Stateless MCP durable state is unavailable".to_string(),
        code: codes::INVALID_REQUEST,
        status: 409,
    })?;
    let status = resp.status();
    if !status.is_success() {
        return Err(AppError {
            message: "Stateless MCP durable state is unavailable".to_string(),
            code: codes::INVALID_REQUEST,
            status: status.as_u16(),
        });
    }
    let raw = resp.text().map_err(|_| AppError {
        message: "Stateless MCP durable state is unavailable".to_string(),
        code: codes::INVALID_REQUEST,
        status: 409,
    })?;
    let val: Value = serde_json::from_str(&raw)
        .map_err(|_| AppError::invalid_payload("Stateless MCP restore journal is invalid"))?;
    parse_participant_journal(&val)
}

fn parse_participant_journal(value: &Value) -> Result<Value, AppError> {
    let Some(obj) = value.as_object() else {
        return Err(AppError::invalid_payload(
            "Stateless MCP restore journal is invalid",
        ));
    };
    let Some(phase) = obj.get("phase").and_then(Value::as_str) else {
        return Err(AppError::invalid_payload(
            "Stateless MCP restore journal is invalid",
        ));
    };
    let mut remapped_out = Map::new();
    if let Some(remapped) = obj.get("remapped").and_then(Value::as_object) {
        for (k, v) in remapped {
            remapped_out.insert(k.clone(), Value::String(value_str(v)));
        }
    }
    Ok(json!({
        "sourceDigest": obj.get("sourceDigest").filter(|v| !v.is_null()).map(value_str).unwrap_or_default(),
        "preparedDigest": obj.get("preparedDigest").filter(|v| !v.is_null()).map(value_str).unwrap_or_default(),
        "phase": phase,
        "imported": obj.get("imported").and_then(Value::as_i64).unwrap_or(0),
        "skipped": obj.get("skipped").and_then(Value::as_i64).unwrap_or(0),
        "interrupted": obj.get("interrupted").and_then(Value::as_i64).unwrap_or(0),
        "remapped": Value::Object(remapped_out),
    }))
}

fn external_prepare_restore(
    restore_id: &str,
    source: &Path,
    tx_digest: &str,
) -> Result<Value, AppError> {
    let snapshot = if source.is_dir() {
        source.join("state.jsonl")
    } else {
        source.to_path_buf()
    };
    let bytes = fs::read(&snapshot)
        .map_err(|_| AppError::invalid_payload("stateless-mcp: snapshot is missing"))?;
    let size = bytes.len().to_string();
    let sha = hex_digest(&Sha256::digest(&bytes));
    stateless_mcp_request(
        &format!("/internal/restores/{restore_id}/prepare"),
        reqwest::Method::POST,
        Some(bytes),
        &[
            ("Content-Length", &size),
            ("X-Content-SHA256", &sha),
            ("X-Transaction-Digest", tx_digest),
        ],
        300,
    )
}

fn external_post_json(path: &str, body_val: &Value, timeout_secs: u64) -> Result<Value, AppError> {
    let raw = serde_json::to_vec(body_val).map_err(|_| route_support::unhandled_error())?;
    stateless_mcp_request(path, reqwest::Method::POST, Some(raw), &[], timeout_secs)
}

fn external_participant_indices(transaction: &Map<String, Value>) -> Vec<usize> {
    let Some(list) = transaction.get("contributors").and_then(Value::as_array) else {
        return Vec::new();
    };
    list.iter()
        .enumerate()
        .filter_map(|(idx, item)| {
            if item.get("external").is_some_and(python_truthy) {
                Some(idx)
            } else {
                None
            }
        })
        .collect()
}

fn external_commit_intent(
    transaction: &mut Map<String, Value>,
    restore_id: &str,
) -> Result<(), AppError> {
    let digest = transaction
        .get("serverTransactionDigest")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let indices = external_participant_indices(transaction);
    for idx in indices {
        let journal = external_post_json(
            &format!("/internal/restores/{restore_id}/commit-intent"),
            &json!({ "transactionDigest": digest }),
            60,
        )?;
        let phase = journal
            .get("phase")
            .cloned()
            .unwrap_or_else(|| Value::String(String::new()));
        if let Some(items) = transaction
            .get_mut("contributors")
            .and_then(Value::as_array_mut)
        {
            if let Some(entry) = items.get_mut(idx).and_then(Value::as_object_mut) {
                entry.insert("participant".into(), journal);
                entry.insert("phase".into(), phase);
            }
        }
    }
    Ok(())
}

fn update_server_fence(
    root: &Path,
    transaction: &Map<String, Value>,
    phase: &str,
) -> Result<(), AppError> {
    let restore_id = transaction
        .get("restoreId")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let existing = mutation_gate::read_fence(root).map_err(gate_to_app_error)?;
    let mut fence = match existing {
        Some(Value::Object(obj))
            if obj.get("restoreId").and_then(Value::as_str) == Some(restore_id) =>
        {
            obj
        }
        _ => {
            let mut m = Map::new();
            m.insert("schemaVersion".into(), json!(1));
            m.insert("restoreId".into(), Value::String(restore_id.to_string()));
            m.insert(
                "previousEpoch".into(),
                transaction
                    .get("previousEpoch")
                    .cloned()
                    .unwrap_or_else(|| Value::String("unknown".to_string())),
            );
            m.insert(
                "targetEpoch".into(),
                transaction
                    .get("targetEpoch")
                    .cloned()
                    .unwrap_or_else(|| Value::String("unknown".to_string())),
            );
            m.insert(
                "ownerDocumentId".into(),
                transaction
                    .get("ownerDocumentId")
                    .cloned()
                    .unwrap_or_else(|| Value::String("server".to_string())),
            );
            m.insert(
                "createdAt".into(),
                transaction
                    .get("createdAt")
                    .cloned()
                    .unwrap_or_else(|| json!(now_unix_ms())),
            );
            m.insert(
                "expiresAt".into(),
                transaction
                    .get("expiresAt")
                    .cloned()
                    .unwrap_or_else(|| json!(UNBOUNDED_EXPIRY_MS)),
            );
            m
        }
    };
    fence.insert("phase".into(), Value::String(phase.to_string()));
    mutation_gate::write_fence(&Value::Object(fence), root).map_err(gate_to_app_error)?;
    Ok(())
}

fn cleanup_restore_payload(restore_dir: &Path, remove_upload: bool) {
    if let Some(name) = restore_dir.file_name().and_then(|v| v.to_str()) {
        clear_secret(name);
    }
    for sub in ["extracted", "verified", "staged", "rollback"] {
        let _ = fs::remove_dir_all(restore_dir.join(sub));
    }
    let _ = fs::remove_file(restore_dir.join("unlocked.dsibackup"));
    if remove_upload {
        if let Ok(entries) = fs::read_dir(restore_dir) {
            for entry in entries.flatten() {
                let p = entry.path();
                if p.is_file() {
                    let lower = entry.file_name().to_string_lossy().to_ascii_lowercase();
                    if lower.ends_with(".dsibackup") || lower.ends_with(".dsibackup.age") {
                        let _ = fs::remove_file(&p);
                    }
                }
            }
        }
        let _ = fs::remove_file(restore_dir.join("upload.json"));
    }
}

fn rollback_transaction(
    root: &Path,
    transaction: &mut Map<String, Value>,
    restore_dir: &Path,
) -> Result<(), AppError> {
    let journal_path = restore_dir.join("transaction.json");
    transaction.insert("phase".into(), Value::String("aborting".to_string()));
    transaction.insert("updatedAt".into(), Value::String(now_utc_iso()));
    write_atomic_json(&journal_path, &Value::Object(transaction.clone()))?;

    let restore_id = transaction
        .get("restoreId")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let count = transaction
        .get("contributors")
        .and_then(Value::as_array)
        .ok_or_else(|| AppError::invalid_payload("Restore contributor journal is invalid"))?
        .len();

    for idx in (0..count).rev() {
        let contributor = transaction
            .get("contributors")
            .and_then(Value::as_array)
            .and_then(|arr| arr.get(idx))
            .and_then(Value::as_object)
            .cloned()
            .ok_or_else(|| AppError::invalid_payload("Restore contributor journal is invalid"))?;

        if contributor.get("external").is_some_and(python_truthy) {
            let outcome = external_post_json(
                &format!("/internal/restores/{restore_id}/abort"),
                &json!({}),
                60,
            );
            if let Some(items) = transaction
                .get_mut("contributors")
                .and_then(Value::as_array_mut)
            {
                if let Some(entry) = items.get_mut(idx).and_then(Value::as_object_mut) {
                    match outcome {
                        Ok(journal) => {
                            let p = journal
                                .get("phase")
                                .filter(|v| python_truthy(v))
                                .cloned()
                                .unwrap_or_else(|| Value::String("rolled-back".to_string()));
                            entry.insert("participant".into(), journal);
                            entry.insert("phase".into(), p);
                            entry.insert(
                                "swapState".into(),
                                Value::String("rolled-back".to_string()),
                            );
                        }
                        Err(_) => {
                            entry.insert(
                                "phase".into(),
                                Value::String("recovery-required".to_string()),
                            );
                            entry.insert(
                                "swapState".into(),
                                Value::String("recovery-required".to_string()),
                            );
                        }
                    }
                }
            }
            write_atomic_json(&journal_path, &Value::Object(transaction.clone()))?;
            continue;
        }

        let destination = PathBuf::from(
            contributor
                .get("destination")
                .and_then(Value::as_str)
                .unwrap_or_default(),
        );
        let rollback = PathBuf::from(
            contributor
                .get("rollbackPath")
                .and_then(Value::as_str)
                .unwrap_or_default(),
        );
        let project_scope: Vec<String> = contributor
            .get("projectScope")
            .and_then(Value::as_array)
            .map(|items| {
                items
                    .iter()
                    .filter(|v| !v.is_null())
                    .map(value_str)
                    .collect()
            })
            .unwrap_or_default();

        if !project_scope.is_empty() {
            let swap_state = contributor
                .get("projectSwapState")
                .and_then(Value::as_object);
            for project in &project_scope {
                let st = swap_state
                    .and_then(|m| m.get(project))
                    .and_then(Value::as_object);
                let installed = st
                    .and_then(|m| m.get("installed"))
                    .is_some_and(python_truthy);
                let had = st.and_then(|m| m.get("had")).is_some_and(python_truthy);
                let dst_proj = destination.join(project);
                let rb_proj = rollback.join(project);
                if installed && dst_proj.exists() {
                    remove_path_if_exists(&dst_proj)?;
                }
                if had && rb_proj.exists() {
                    if let Some(parent) = dst_proj.parent() {
                        fs::create_dir_all(parent).map_err(|_| route_support::unhandled_error())?;
                    }
                    fs::rename(&rb_proj, &dst_proj)
                        .map_err(|_| route_support::unhandled_error())?;
                }
            }
        } else {
            let state = contributor
                .get("swapState")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let installed = contributor.get("swapped").is_some_and(python_truthy)
                || matches!(state, "installing-staged" | "swapped");
            let old_moved =
                matches!(state, "old-moved" | "installing-staged" | "swapped") || rollback.exists();
            if installed && destination.exists() {
                remove_path_if_exists(&destination)?;
            }
            let had_dst = contributor.get("hadDestination").is_some_and(python_truthy);
            if had_dst && old_moved && rollback.exists() {
                if let Some(parent) = destination.parent() {
                    fs::create_dir_all(parent).map_err(|_| route_support::unhandled_error())?;
                }
                fs::rename(&rollback, &destination)
                    .map_err(|_| route_support::unhandled_error())?;
            }
        }

        if let Some(items) = transaction
            .get_mut("contributors")
            .and_then(Value::as_array_mut)
        {
            if let Some(entry) = items.get_mut(idx).and_then(Value::as_object_mut) {
                entry.insert("swapped".into(), Value::Bool(false));
                entry.insert("swapState".into(), Value::String("rolled-back".to_string()));
                entry.insert("phase".into(), Value::String("rolled-back".to_string()));
            }
        }
        write_atomic_json(&journal_path, &Value::Object(transaction.clone()))?;
    }

    let _ = fs::remove_dir_all(restore_dir.join("staged"));
    let recovery_required = transaction
        .get("contributors")
        .and_then(Value::as_array)
        .is_some_and(|items| {
            items
                .iter()
                .any(|c| c.get("phase").and_then(Value::as_str) == Some("recovery-required"))
        });
    if recovery_required {
        transaction.insert(
            "phase".into(),
            Value::String("recovery-required".to_string()),
        );
        transaction.insert("updatedAt".into(), Value::String(now_utc_iso()));
        write_atomic_json(&journal_path, &Value::Object(transaction.clone()))?;
        update_server_fence(root, transaction, "recovery-required")?;
        return Ok(());
    }
    transaction.insert("phase".into(), Value::String("rolled-back".to_string()));
    transaction.insert("rolledBackAt".into(), Value::String(now_utc_iso()));
    transaction.insert("updatedAt".into(), Value::String(now_utc_iso()));
    write_atomic_json(&journal_path, &Value::Object(transaction.clone()))?;
    mutation_gate::clear_fence(&restore_id, root).map_err(gate_to_app_error)?;
    mutation_gate::bump_generation(root).map_err(gate_to_app_error)?;
    Ok(())
}

pub(crate) fn prepare_restore(
    root: &Path,
    restore_id: &str,
    payload: &Map<String, Value>,
) -> Result<Value, AppError> {
    let mode = payload
        .get("mode")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "merge".to_string());
    if !is_valid_mode(&mode) {
        return Err(AppError::invalid_payload("Unsupported restore mode"));
    }
    let previous_epoch = payload
        .get("previousEpoch")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "legacy".to_string());
    let target_epoch = payload
        .get("targetEpoch")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .filter(|s| !s.is_empty());
    let owner_document_id = payload
        .get("ownerDocumentId")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "browser".to_string());

    let restore_dir = restore_root(root, restore_id)?;
    let journal_path = restore_dir.join("transaction.json");
    let plan = read_json_object(&restore_dir.join("plan.json"))?;

    if journal_path.is_file() {
        let existing = read_json_object(&journal_path)?;
        if existing.get("phase").and_then(Value::as_str) == Some("failed") {
            let _ = fs::remove_file(&journal_path);
        } else {
            let same_mode = existing.get("mode").and_then(Value::as_str) == Some(mode.as_str());
            let same_target = match target_epoch.as_deref() {
                Some(t) => existing.get("targetEpoch").and_then(Value::as_str) == Some(t),
                None => true,
            };
            let same_prev = existing.get("previousEpoch").and_then(Value::as_str)
                == Some(previous_epoch.as_str());
            if !same_mode || !same_target || !same_prev {
                return Err(AppError {
                    message: "Restore retry parameters do not match the durable transaction"
                        .to_string(),
                    code: codes::INVALID_REQUEST,
                    status: 409,
                });
            }
            let ex_sel = existing
                .get("selectionDigest")
                .filter(|v| python_truthy(v))
                .map(value_str)
                .unwrap_or_default();
            let pl_sel = plan
                .get("selectionDigest")
                .filter(|v| python_truthy(v))
                .map(value_str)
                .unwrap_or_default();
            if ex_sel != pl_sel {
                return Err(AppError {
                    message: "Restore selection does not match the durable transaction".to_string(),
                    code: codes::INVALID_REQUEST,
                    status: 409,
                });
            }
            return public_restore(&existing, &restore_dir, true);
        }
    }

    if !plan.get("compatible").is_some_and(python_truthy) {
        return Err(AppError {
            message: "Backup schema is not compatible with this version".to_string(),
            code: codes::INVALID_REQUEST,
            status: 409,
        });
    }

    let projected = plan.get("projected").is_some_and(python_truthy);
    let mut archive_path: Option<PathBuf> = None;
    let mut initial_manifest: Option<Value> = None;
    if projected {
        let Some(raw_manifest) = plan.get("manifest") else {
            return Err(AppError::invalid_payload(
                "Projected restore plan is missing its manifest",
            ));
        };
        if !raw_manifest.is_object() {
            return Err(AppError::invalid_payload(
                "Projected restore plan is missing its manifest",
            ));
        }
        initial_manifest = Some(raw_manifest.clone());
    } else {
        let mut found_archive: Option<PathBuf> = None;
        if !restore_dir.join("remote-fetch.json").is_file() {
            if let Ok(entries) = fs::read_dir(&restore_dir) {
                let mut names: Vec<PathBuf> = entries
                    .flatten()
                    .map(|e| e.path())
                    .filter(|p| {
                        p.is_file()
                            && p.file_name()
                                .and_then(|n| n.to_str())
                                .is_some_and(|n| n.ends_with(".dsibackup"))
                    })
                    .collect();
                names.sort();
                found_archive = names.into_iter().next();
            }
        }
        if found_archive.is_none() && plan.get("encrypted").is_some_and(python_truthy) {
            initial_manifest = Some(verified_extracted_manifest(&restore_dir, &plan)?);
        } else if let Some(ref arch) = found_archive {
            let actual_sha = sha256_file(arch)
                .map_err(|_| AppError::invalid_payload("Backup changed after inspection"))?;
            let expected_sha = plan
                .get("archiveSha256")
                .and_then(Value::as_str)
                .unwrap_or("");
            if actual_sha != expected_sha {
                return Err(AppError::invalid_payload("Backup changed after inspection"));
            }
            archive_path = found_archive;
        } else {
            return Err(AppError::invalid_payload("Backup changed after inspection"));
        }
    }

    let target = match target_epoch {
        Some(t) => t,
        None => format!("epoch-{}", format_uuid_like()?),
    };
    let now_ms = now_unix_ms();
    let fence = json!({
        "schemaVersion": 1,
        "restoreId": restore_id,
        "previousEpoch": previous_epoch,
        "targetEpoch": target,
        "ownerDocumentId": owner_document_id,
        "phase": "preparing",
        "createdAt": now_ms,
        "expiresAt": now_ms.saturating_add(FENCE_TTL_MS),
    });

    let _gate = mutation_gate::exclusive_gate(root).map_err(gate_to_app_error)?;
    if let Some(Value::Object(active)) =
        mutation_gate::read_fence(root).map_err(gate_to_app_error)?
    {
        if active.get("restoreId").and_then(Value::as_str) != Some(restore_id) {
            return Err(AppError {
                message: "Another workspace restore is active".to_string(),
                code: codes::INVALID_REQUEST,
                status: 423,
            });
        }
    }
    mutation_gate::write_fence(&fence, root).map_err(gate_to_app_error)?;

    let requires_frontend = plan.get("requiresFrontendApply").is_some_and(python_truthy);
    let requires_external = plan.get("requiresExternalMcp").is_some_and(python_truthy);
    let selection_digest = plan
        .get("selectionDigest")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();

    let mut transaction = fence.as_object().cloned().unwrap_or_default();
    transaction.insert("mode".into(), Value::String(mode.clone()));
    transaction.insert("phase".into(), Value::String("preparing".to_string()));
    transaction.insert("updatedAt".into(), Value::String(now_utc_iso()));
    transaction.insert(
        "requiresFrontendApply".into(),
        Value::Bool(requires_frontend),
    );
    transaction.insert("requiresExternalMcp".into(), Value::Bool(requires_external));
    transaction.insert(
        "selectionDigest".into(),
        Value::String(selection_digest.clone()),
    );
    transaction.insert("contributors".into(), Value::Array(Vec::new()));
    transaction.insert("identityMap".into(), Value::Object(Map::new()));
    write_atomic_json(&journal_path, &Value::Object(transaction.clone()))?;

    let attempt: Result<(), AppError> = (|| {
        if initial_manifest.is_none() {
            let arch = archive_path
                .as_ref()
                .ok_or_else(|| AppError::invalid_payload("Backup changed after inspection"))?;
            let verified_dir = restore_dir.join("verified");
            let _ = fs::remove_dir_all(&verified_dir);
            initial_manifest = Some(safe_extract_and_verify(arch, &verified_dir)?);
        }
        let manifest_val = initial_manifest
            .as_ref()
            .ok_or_else(|| AppError::invalid_payload("Backup manifest missing"))?;
        let context_project_ids: Vec<String> = manifest_val
            .get("scope")
            .and_then(|s| s.get("projectIds"))
            .and_then(Value::as_array)
            .map(|items| {
                items
                    .iter()
                    .filter(|v| !v.is_null())
                    .map(value_str)
                    .collect()
            })
            .unwrap_or_default();
        let source_backup_id = plan
            .get("manifest")
            .and_then(|m| m.get("backupId"))
            .or_else(|| manifest_val.get("backupId"))
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();

        let identity_map = restore_identity_map(&plan, &restore_dir, &mode, projected)?;
        let identity_json: Map<String, Value> = identity_map
            .iter()
            .map(|(k, v)| (k.clone(), Value::String(v.clone())))
            .collect();
        transaction.insert("identityMap".into(), Value::Object(identity_json));

        let safety = create_safety_backup(root, &plan, restore_id)?;
        if let Some(backup_id) = safety.get("backupId").cloned() {
            transaction.insert("safetyBackupId".into(), backup_id);
        }
        write_atomic_json(&journal_path, &Value::Object(transaction.clone()))?;

        let staged_root = restore_dir.join("staged");
        let _ = fs::remove_dir_all(&staged_root);
        fs::create_dir_all(&staged_root).map_err(|_| route_support::unhandled_error())?;

        let source_base = restore_dir.join(if projected { "extracted" } else { "verified" });
        let operations = plan
            .get("operations")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();

        for operation in operations {
            let Some(cid) = operation.get("contributorId").and_then(Value::as_str) else {
                return Err(AppError::invalid_payload(
                    "Backup contributor inventory is invalid",
                ));
            };
            if operation.get("external").is_some_and(python_truthy) {
                let source = source_base.join("payload").join(cid);
                validate_staged_contributor(cid, &source)?;
                let digest = safe_tree_digest(&source)?;
                let entry = json!({
                    "id": cid,
                    "external": true,
                    "source": source.to_string_lossy().into_owned(),
                    "prepared": false,
                    "swapped": false,
                    "verified": true,
                    "digest": digest,
                    "swapState": "validated",
                    "phase": "preparing",
                });
                if let Some(arr) = transaction
                    .get_mut("contributors")
                    .and_then(Value::as_array_mut)
                {
                    arr.push(entry);
                }
                write_atomic_json(&journal_path, &Value::Object(transaction.clone()))?;
                continue;
            }

            if known_contributor(cid).is_none() {
                return Err(AppError::invalid_payload(format!(
                    "Unsupported contributor: {cid}"
                )));
            }
            let destination = PathBuf::from(
                operation
                    .get("destination")
                    .and_then(Value::as_str)
                    .unwrap_or_default(),
            );
            let staged = staged_root.join(cid);
            let project_scope: Vec<String> = operation
                .get("projectScope")
                .and_then(Value::as_array)
                .map(|items| {
                    items
                        .iter()
                        .filter(|v| !v.is_null())
                        .map(value_str)
                        .collect()
                })
                .unwrap_or_default();
            if !project_scope.is_empty() {
                fs::create_dir_all(&staged).map_err(|_| route_support::unhandled_error())?;
            } else if destination.exists() {
                copy_tree_recursive(&destination, &staged)?;
            } else {
                fs::create_dir_all(&staged).map_err(|_| route_support::unhandled_error())?;
            }

            let mut op_identity_map = identity_map.clone();
            for proj in &project_scope {
                op_identity_map.insert(proj.clone(), proj.clone());
            }
            let payload_src = source_base.join("payload").join(cid);
            apply_restore_into_staging(&StagingMergePlan {
                contributor_id: cid,
                source_dir: &payload_src,
                staged_dir: &staged,
                mode: &mode,
                identity_map: &op_identity_map,
                source_backup_id: &source_backup_id,
                context_project_ids: &context_project_ids,
            })?;
            validate_staged_contributor(cid, &staged)?;
            fsync_tree(&staged);

            let had_destination = destination.exists();
            let rollback_path = restore_dir.join("rollback").join(cid);
            let digest = safe_tree_digest(&staged)?;
            let mut entry = Map::new();
            entry.insert("id".into(), Value::String(cid.to_string()));
            entry.insert(
                "destination".into(),
                Value::String(destination.to_string_lossy().into_owned()),
            );
            entry.insert(
                "stagedPath".into(),
                Value::String(staged.to_string_lossy().into_owned()),
            );
            entry.insert(
                "rollbackPath".into(),
                Value::String(rollback_path.to_string_lossy().into_owned()),
            );
            entry.insert("hadDestination".into(), Value::Bool(had_destination));
            if project_scope.is_empty() {
                entry.insert("projectScope".into(), Value::Null);
            } else {
                entry.insert(
                    "projectScope".into(),
                    Value::Array(project_scope.into_iter().map(Value::String).collect()),
                );
            }
            entry.insert("prepared".into(), Value::Bool(true));
            entry.insert("swapped".into(), Value::Bool(false));
            entry.insert("verified".into(), Value::Bool(true));
            entry.insert("digest".into(), Value::String(digest));
            entry.insert("swapState".into(), Value::String("prepared".to_string()));

            if let Some(arr) = transaction
                .get_mut("contributors")
                .and_then(Value::as_array_mut)
            {
                arr.push(Value::Object(entry));
            }
            write_atomic_json(&journal_path, &Value::Object(transaction.clone()))?;
        }

        let summary_contribs: Vec<Value> = transaction
            .get("contributors")
            .and_then(Value::as_array)
            .map(|items| {
                items
                    .iter()
                    .map(|c| {
                        json!({
                            "id": c.get("id").cloned().unwrap_or(Value::Null),
                            "digest": c.get("digest").cloned().unwrap_or(Value::Null),
                        })
                    })
                    .collect()
            })
            .unwrap_or_default();
        let tx_digest = sha256_compact_json(&json!({
            "restoreId": restore_id,
            "mode": mode,
            "targetEpoch": target,
            "selectionDigest": selection_digest,
            "contributors": summary_contribs,
        }));
        transaction.insert(
            "serverTransactionDigest".into(),
            Value::String(tx_digest.clone()),
        );

        let ext_indices = external_participant_indices(&transaction);
        for idx in ext_indices {
            let source_path = transaction
                .get("contributors")
                .and_then(Value::as_array)
                .and_then(|items| items.get(idx))
                .and_then(|c| c.get("source"))
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            let journal =
                external_prepare_restore(restore_id, Path::new(&source_path), &tx_digest)?;
            let ext_phase = journal
                .get("phase")
                .cloned()
                .unwrap_or_else(|| Value::String(String::new()));
            if let Some(items) = transaction
                .get_mut("contributors")
                .and_then(Value::as_array_mut)
            {
                if let Some(entry) = items.get_mut(idx).and_then(Value::as_object_mut) {
                    entry.insert("transactionDigest".into(), Value::String(tx_digest.clone()));
                    entry.insert("participant".into(), journal);
                    entry.insert("phase".into(), ext_phase);
                    entry.insert("prepared".into(), Value::Bool(true));
                    entry.insert("swapState".into(), Value::String("prepared".to_string()));
                }
            }
            write_atomic_json(&journal_path, &Value::Object(transaction.clone()))?;
        }

        transaction.insert("phase".into(), Value::String("backend-staged".to_string()));
        transaction.insert("updatedAt".into(), Value::String(now_utc_iso()));
        let next_fence_phase = if requires_frontend {
            "preparing"
        } else {
            "commit-intent"
        };
        let mut next_fence = fence.clone();
        if let Some(obj) = next_fence.as_object_mut() {
            obj.insert("phase".into(), Value::String(next_fence_phase.to_string()));
        }
        mutation_gate::write_fence(&next_fence, root).map_err(gate_to_app_error)?;
        write_atomic_json(&journal_path, &Value::Object(transaction.clone()))?;
        Ok(())
    })();

    if let Err(err) = attempt {
        transaction.insert("phase".into(), Value::String("failed".to_string()));
        transaction.insert("error".into(), Value::String(err.message.clone()));
        transaction.insert("updatedAt".into(), Value::String(now_utc_iso()));
        let _ = write_atomic_json(&journal_path, &Value::Object(transaction));
        let _ = mutation_gate::clear_fence(restore_id, root);
        if !err.message.contains("required or expired") {
            cleanup_restore_payload(&restore_dir, true);
        }
        return Err(err);
    }
    public_restore(&transaction, &restore_dir, true)
}

pub(crate) fn frontend_prepared(
    root: &Path,
    restore_id: &str,
    digest: &str,
) -> Result<Value, AppError> {
    let restore_dir = restore_root(root, restore_id)?;
    let journal_path = restore_dir.join("transaction.json");
    let mut transaction = read_json_object(&journal_path)?;
    let phase = transaction
        .get("phase")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if matches!(
        phase,
        "frontend-staged"
            | "commit-intent"
            | "frontend-committed"
            | "backend-committed"
            | "complete"
    ) {
        let existing_digest = transaction.get("frontendDigest").and_then(Value::as_str);
        if let Some(existing) = existing_digest {
            if existing != digest {
                return Err(AppError {
                    message: "Frontend restore digest changed".to_string(),
                    code: codes::INVALID_REQUEST,
                    status: 409,
                });
            }
        }
        return public_restore(&transaction, &restore_dir, true);
    }
    if phase != "backend-staged"
        || !transaction
            .get("requiresFrontendApply")
            .is_some_and(python_truthy)
    {
        return Err(AppError {
            message: "Restore is not waiting for frontend staging".to_string(),
            code: codes::INVALID_REQUEST,
            status: 409,
        });
    }
    if !is_64_hex_lowercase(digest) {
        return Err(AppError::invalid_payload(
            "Frontend restore digest is invalid",
        ));
    }
    transaction.insert(
        "frontendDigest".into(),
        Value::String(digest.to_ascii_lowercase()),
    );
    transaction.insert("phase".into(), Value::String("frontend-staged".to_string()));
    transaction.insert("updatedAt".into(), Value::String(now_utc_iso()));
    write_atomic_json(&journal_path, &Value::Object(transaction.clone()))?;
    update_server_fence(root, &transaction, "frontend-staged")?;
    public_restore(&transaction, &restore_dir, true)
}

pub(crate) fn commit_restore(
    root: &Path,
    restore_id: &str,
    frontend_committed: bool,
    frontend_digest: Option<&str>,
) -> Result<Value, AppError> {
    let restore_dir = restore_root(root, restore_id)?;
    let journal_path = restore_dir.join("transaction.json");
    let _gate = mutation_gate::exclusive_gate(root).map_err(gate_to_app_error)?;

    let mut transaction = read_json_object(&journal_path)?;
    let phase = transaction
        .get("phase")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    if phase == "backend-committed" || phase == "complete" {
        return public_restore(&transaction, &restore_dir, true);
    }

    let requires_frontend = transaction
        .get("requiresFrontendApply")
        .is_some_and(python_truthy);
    if requires_frontend {
        if let Some(fd) = frontend_digest {
            if transaction.get("frontendDigest").and_then(Value::as_str) != Some(fd) {
                return Err(AppError {
                    message: "Frontend restore digest does not match staged state".to_string(),
                    code: codes::INVALID_REQUEST,
                    status: 409,
                });
            }
        }
        if phase == "frontend-staged" {
            transaction.insert("phase".into(), Value::String("commit-intent".to_string()));
            transaction.insert("commitIntentAt".into(), Value::String(now_utc_iso()));
            transaction.insert("updatedAt".into(), Value::String(now_utc_iso()));
            write_atomic_json(&journal_path, &Value::Object(transaction.clone()))?;
            update_server_fence(root, &transaction, "commit-intent")?;
            external_commit_intent(&mut transaction, restore_id)?;
            write_atomic_json(&journal_path, &Value::Object(transaction.clone()))?;
            if !frontend_committed {
                return public_restore(&transaction, &restore_dir, true);
            }
        } else if phase != "commit-intent" {
            return Err(AppError {
                message: "Restore is not ready to commit".to_string(),
                code: codes::INVALID_REQUEST,
                status: 409,
            });
        }
        if !frontend_committed {
            return public_restore(&transaction, &restore_dir, true);
        }
        transaction.insert(
            "phase".into(),
            Value::String("frontend-committed".to_string()),
        );
        transaction.insert("frontendCommittedAt".into(), Value::String(now_utc_iso()));
        write_atomic_json(&journal_path, &Value::Object(transaction.clone()))?;
        update_server_fence(root, &transaction, "frontend-committed")?;
    } else if phase == "backend-staged" {
        transaction.insert("phase".into(), Value::String("commit-intent".to_string()));
        transaction.insert("commitIntentAt".into(), Value::String(now_utc_iso()));
        write_atomic_json(&journal_path, &Value::Object(transaction.clone()))?;
        update_server_fence(root, &transaction, "commit-intent")?;
        external_commit_intent(&mut transaction, restore_id)?;
        write_atomic_json(&journal_path, &Value::Object(transaction.clone()))?;
    } else if phase != "commit-intent" && phase != "frontend-committed" {
        return Err(AppError {
            message: "Restore is not ready to commit".to_string(),
            code: codes::INVALID_REQUEST,
            status: 409,
        });
    }

    let swap_attempt: Result<(), AppError> = (|| {
        let rollback_root = restore_dir.join("rollback");
        fs::create_dir_all(&rollback_root).map_err(|_| route_support::unhandled_error())?;

        let contribs = transaction
            .get("contributors")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let mut order: Vec<usize> = contribs
            .iter()
            .enumerate()
            .filter_map(|(i, c)| (!c.get("external").is_some_and(python_truthy)).then_some(i))
            .collect();
        order.extend(
            contribs
                .iter()
                .enumerate()
                .filter_map(|(i, c)| c.get("external").is_some_and(python_truthy).then_some(i)),
        );

        let tx_digest = transaction
            .get("serverTransactionDigest")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();

        for idx in order {
            let contributor = transaction
                .get("contributors")
                .and_then(Value::as_array)
                .and_then(|arr| arr.get(idx))
                .and_then(Value::as_object)
                .cloned()
                .ok_or_else(route_support::unhandled_error)?;

            if contributor.get("external").is_some_and(python_truthy) {
                set_contributor_field(
                    &mut transaction,
                    idx,
                    "swapState",
                    Value::String("committing-external".to_string()),
                );
                write_atomic_json(&journal_path, &Value::Object(transaction.clone()))?;
                let journal = external_post_json(
                    &format!("/internal/restores/{restore_id}/commit"),
                    &json!({ "transactionDigest": tx_digest }),
                    300,
                )?;
                let ext_phase = journal
                    .get("phase")
                    .cloned()
                    .unwrap_or_else(|| Value::String(String::new()));
                if let Some(items) = transaction
                    .get_mut("contributors")
                    .and_then(Value::as_array_mut)
                {
                    if let Some(entry) = items.get_mut(idx).and_then(Value::as_object_mut) {
                        entry.insert("participant".into(), journal);
                        entry.insert("phase".into(), ext_phase);
                        entry.insert("swapped".into(), Value::Bool(true));
                        entry.insert("swapState".into(), Value::String("swapped".to_string()));
                    }
                }
                write_atomic_json(&journal_path, &Value::Object(transaction.clone()))?;
                continue;
            }

            let destination = PathBuf::from(
                contributor
                    .get("destination")
                    .and_then(Value::as_str)
                    .unwrap_or_default(),
            );
            let staged = PathBuf::from(
                contributor
                    .get("stagedPath")
                    .and_then(Value::as_str)
                    .unwrap_or_default(),
            );
            let rollback = PathBuf::from(
                contributor
                    .get("rollbackPath")
                    .and_then(Value::as_str)
                    .unwrap_or_default(),
            );
            let project_scope: Vec<String> = contributor
                .get("projectScope")
                .and_then(Value::as_array)
                .map(|items| {
                    items
                        .iter()
                        .filter(|v| !v.is_null())
                        .map(value_str)
                        .collect()
                })
                .unwrap_or_default();

            if !project_scope.is_empty() {
                set_contributor_field(
                    &mut transaction,
                    idx,
                    "projectSwapState",
                    Value::Object(Map::new()),
                );
                for project in &project_scope {
                    let dst_proj = destination.join(project);
                    let stg_proj = staged.join(project);
                    let rb_proj = rollback.join(project);
                    let had = dst_proj.exists();
                    let mut installed = false;
                    set_project_swap_state(&mut transaction, idx, project, had, installed);
                    write_atomic_json(&journal_path, &Value::Object(transaction.clone()))?;
                    if had {
                        if let Some(parent) = rb_proj.parent() {
                            fs::create_dir_all(parent)
                                .map_err(|_| route_support::unhandled_error())?;
                        }
                        remove_path_if_exists(&rb_proj)?;
                        fs::rename(&dst_proj, &rb_proj)
                            .map_err(|_| route_support::unhandled_error())?;
                        if let Some(parent) = dst_proj.parent() {
                            fsync_dir(parent);
                        }
                    }
                    if stg_proj.exists() {
                        if let Some(parent) = dst_proj.parent() {
                            fs::create_dir_all(parent)
                                .map_err(|_| route_support::unhandled_error())?;
                        }
                        fs::rename(&stg_proj, &dst_proj)
                            .map_err(|_| route_support::unhandled_error())?;
                        if let Some(parent) = dst_proj.parent() {
                            fsync_dir(parent);
                        }
                        installed = true;
                    }
                    set_project_swap_state(&mut transaction, idx, project, had, installed);
                    write_atomic_json(&journal_path, &Value::Object(transaction.clone()))?;
                }
                set_contributor_field(&mut transaction, idx, "swapped", Value::Bool(true));
                set_contributor_field(
                    &mut transaction,
                    idx,
                    "swapState",
                    Value::String("swapped".to_string()),
                );
                write_atomic_json(&journal_path, &Value::Object(transaction.clone()))?;
                continue;
            }

            set_contributor_field(
                &mut transaction,
                idx,
                "swapState",
                Value::String("moving-old".to_string()),
            );
            write_atomic_json(&journal_path, &Value::Object(transaction.clone()))?;
            let had_dst = contributor.get("hadDestination").is_some_and(python_truthy);
            if had_dst && destination.exists() {
                if let Some(parent) = rollback.parent() {
                    fs::create_dir_all(parent).map_err(|_| route_support::unhandled_error())?;
                }
                remove_path_if_exists(&rollback)?;
                fs::rename(&destination, &rollback)
                    .map_err(|_| route_support::unhandled_error())?;
                if let Some(parent) = destination.parent() {
                    fsync_dir(parent);
                }
            }
            set_contributor_field(
                &mut transaction,
                idx,
                "swapState",
                Value::String("old-moved".to_string()),
            );
            write_atomic_json(&journal_path, &Value::Object(transaction.clone()))?;
            if let Some(parent) = destination.parent() {
                fs::create_dir_all(parent).map_err(|_| route_support::unhandled_error())?;
            }
            set_contributor_field(
                &mut transaction,
                idx,
                "swapState",
                Value::String("installing-staged".to_string()),
            );
            write_atomic_json(&journal_path, &Value::Object(transaction.clone()))?;
            fs::rename(&staged, &destination).map_err(|_| route_support::unhandled_error())?;
            if let Some(parent) = destination.parent() {
                fsync_dir(parent);
            }
            set_contributor_field(&mut transaction, idx, "swapped", Value::Bool(true));
            set_contributor_field(
                &mut transaction,
                idx,
                "swapState",
                Value::String("swapped".to_string()),
            );
            write_atomic_json(&journal_path, &Value::Object(transaction.clone()))?;
        }

        transaction.insert(
            "phase".into(),
            Value::String("backend-committed".to_string()),
        );
        transaction.insert("backendCommittedAt".into(), Value::String(now_utc_iso()));
        transaction.insert("updatedAt".into(), Value::String(now_utc_iso()));
        write_atomic_json(&journal_path, &Value::Object(transaction.clone()))?;
        update_server_fence(root, &transaction, "backend-committed")?;
        mutation_gate::bump_generation(root).map_err(gate_to_app_error)?;
        Ok(())
    })();

    if let Err(err) = swap_attempt {
        let _ = rollback_transaction(root, &mut transaction, &restore_dir);
        return Err(err);
    }
    public_restore(&transaction, &restore_dir, true)
}

fn set_contributor_field(transaction: &mut Map<String, Value>, idx: usize, key: &str, val: Value) {
    if let Some(items) = transaction
        .get_mut("contributors")
        .and_then(Value::as_array_mut)
    {
        if let Some(entry) = items.get_mut(idx).and_then(Value::as_object_mut) {
            entry.insert(key.into(), val);
        }
    }
}

fn set_project_swap_state(
    transaction: &mut Map<String, Value>,
    idx: usize,
    project: &str,
    had: bool,
    installed: bool,
) {
    if let Some(items) = transaction
        .get_mut("contributors")
        .and_then(Value::as_array_mut)
    {
        if let Some(entry) = items.get_mut(idx).and_then(Value::as_object_mut) {
            let map = entry
                .entry("projectSwapState".to_string())
                .or_insert_with(|| Value::Object(Map::new()));
            if let Some(inner) = map.as_object_mut() {
                inner.insert(
                    project.to_string(),
                    json!({ "had": had, "installed": installed }),
                );
            }
        }
    }
}

pub(crate) fn complete_restore(
    root: &Path,
    restore_id: &str,
    frontend_digest: Option<&str>,
) -> Result<Value, AppError> {
    let restore_dir = restore_root(root, restore_id)?;
    let journal_path = restore_dir.join("transaction.json");
    let _gate = mutation_gate::exclusive_gate(root).map_err(gate_to_app_error)?;

    let mut transaction = read_json_object(&journal_path)?;
    if transaction.get("phase").and_then(Value::as_str) == Some("complete") {
        let result = public_restore(&transaction, &restore_dir, true)?;
        cleanup_restore_payload(&restore_dir, true);
        return Ok(result);
    }
    if transaction.get("phase").and_then(Value::as_str) != Some("backend-committed") {
        return Err(AppError {
            message: "Backend restore is not committed".to_string(),
            code: codes::INVALID_REQUEST,
            status: 409,
        });
    }
    if transaction
        .get("requiresFrontendApply")
        .is_some_and(python_truthy)
    {
        let expected = transaction.get("frontendDigest").and_then(Value::as_str);
        if frontend_digest != expected {
            return Err(AppError {
                message: "Frontend completion digest does not match".to_string(),
                code: codes::INVALID_REQUEST,
                status: 409,
            });
        }
    }

    let ext_indices = external_participant_indices(&transaction);
    for idx in ext_indices {
        let pending = transaction
            .get("contributors")
            .and_then(Value::as_array)
            .and_then(|arr| arr.get(idx))
            .and_then(|c| c.get("phase"))
            .and_then(Value::as_str)
            == Some("committed-pending-complete");
        if pending {
            let journal = external_post_json(
                &format!("/internal/restores/{restore_id}/complete"),
                &json!({}),
                60,
            )?;
            let ext_phase = journal
                .get("phase")
                .cloned()
                .unwrap_or_else(|| Value::String(String::new()));
            if let Some(items) = transaction
                .get_mut("contributors")
                .and_then(Value::as_array_mut)
            {
                if let Some(entry) = items.get_mut(idx).and_then(Value::as_object_mut) {
                    entry.insert("participant".into(), journal);
                    entry.insert("phase".into(), ext_phase);
                }
            }
        }
    }

    transaction.insert("phase".into(), Value::String("complete".to_string()));
    transaction.insert("completedAt".into(), Value::String(now_utc_iso()));
    transaction.insert("updatedAt".into(), Value::String(now_utc_iso()));
    write_atomic_json(&journal_path, &Value::Object(transaction.clone()))?;
    mutation_gate::clear_fence(restore_id, root).map_err(gate_to_app_error)?;
    let result = public_restore(&transaction, &restore_dir, true)?;
    cleanup_restore_payload(&restore_dir, true);
    Ok(result)
}

pub(crate) fn abort_restore(root: &Path, restore_id: &str) -> Result<Value, AppError> {
    let restore_dir = restore_root(root, restore_id)?;
    let _gate = mutation_gate::exclusive_gate(root).map_err(gate_to_app_error)?;
    let mut transaction = read_json_object(&restore_dir.join("transaction.json"))?;
    let phase = transaction
        .get("phase")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if phase == "rolled-back" {
        let result = public_restore(&transaction, &restore_dir, true)?;
        cleanup_restore_payload(&restore_dir, true);
        return Ok(result);
    }
    if phase == "complete" {
        return Err(AppError {
            message: "Completed restore cannot be aborted".to_string(),
            code: codes::INVALID_REQUEST,
            status: 409,
        });
    }
    rollback_transaction(root, &mut transaction, &restore_dir)?;
    let result = public_restore(&transaction, &restore_dir, true)?;
    cleanup_restore_payload(&restore_dir, true);
    Ok(result)
}

pub(crate) fn apply_restore(root: &Path, restore_id: &str, mode: &str) -> Result<Value, AppError> {
    if !is_valid_mode(mode) {
        return Err(AppError::invalid_payload("Unsupported restore mode"));
    }
    let restore_dir = restore_root(root, restore_id)?;
    let plan = read_json_object(&restore_dir.join("plan.json"))?;
    if plan.get("requiresFrontendApply").is_some_and(python_truthy) {
        return Err(AppError {
            message:
                "Frontend replica acknowledgement is required; use the coordinated restore API"
                    .to_string(),
            code: codes::INVALID_REQUEST,
            status: 409,
        });
    }
    let mut prep = Map::new();
    prep.insert("mode".into(), Value::String(mode.to_string()));
    prep.insert(
        "ownerDocumentId".into(),
        Value::String("server".to_string()),
    );
    prepare_restore(root, restore_id, &prep)?;
    commit_restore(root, restore_id, false, None)?;
    let completed = complete_restore(root, restore_id, None)?;
    let mut out = completed.as_object().cloned().unwrap_or_default();
    out.insert("phase".into(), Value::String("complete".to_string()));
    out.insert("restoreEpoch".into(), json!(now_unix_ms()));
    Ok(Value::Object(out))
}

pub(crate) fn remote_session_path(root: &Path, restore_id: &str) -> Result<PathBuf, AppError> {
    safe_restore_session_id(restore_id)?;
    Ok(root
        .join(".restore-staging")
        .join(restore_id)
        .join("remote-fetch.json"))
}

pub(crate) fn read_remote_restore_session(
    root: &Path,
    restore_id: &str,
) -> Result<Option<Map<String, Value>>, AppError> {
    let path = remote_session_path(root, restore_id)?;
    if !path.is_file() {
        return Ok(None);
    }
    let raw = fs::read_to_string(&path).map_err(|_| route_support::unhandled_error())?;
    let parsed: Value = serde_json::from_str(&raw)
        .map_err(|_| AppError::invalid_payload("Invalid restore session"))?;
    match parsed {
        Value::Object(obj) => Ok(Some(obj)),
        _ => Ok(None),
    }
}

fn is_terminal_remote_phase(phase: &str) -> bool {
    matches!(phase, "complete" | "aborted" | "rolled-back" | "failed")
}

fn is_controlled_stop_phase(phase: &str) -> bool {
    matches!(
        phase,
        "paused" | "aborted" | "rolled-back" | "recovery-required"
    )
}

fn is_uncertain_tx_phase(phase: &str) -> bool {
    matches!(
        phase,
        "commit-intent"
            | "frontend-committed"
            | "backend-committed"
            | "committing"
            | "recovery-required"
    )
}

fn sanitize_telemetry_value(raw: Option<&Value>, fallback_phase: &str) -> Value {
    let obj = raw.and_then(Value::as_object);
    let mut counters = Map::new();
    if let Some(raw_c) = obj
        .and_then(|m| m.get("counters"))
        .and_then(Value::as_object)
    {
        for (k, v) in raw_c {
            if let Some(n) = v.as_i64() {
                if n > 0 {
                    counters.insert(k.clone(), json!(n));
                }
            }
        }
    }
    let mut samples: Vec<Value> = obj
        .and_then(|m| m.get("samples"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if samples.len() > REMOTE_SESSION_MAX_STAGE_SAMPLES {
        samples = samples.split_off(samples.len() - REMOTE_SESSION_MAX_STAGE_SAMPLES);
    }
    let phase = obj
        .and_then(|m| m.get("currentPhase"))
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .unwrap_or(fallback_phase);
    json!({
        "schemaVersion": 1,
        "counters": Value::Object(counters),
        "samples": Value::Array(samples),
        "currentPhase": phase,
    })
}

fn update_telemetry_for_persist(existing: &Map<String, Value>, payload: &mut Map<String, Value>) {
    let next_phase = payload
        .get("phase")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .unwrap_or("unknown")
        .to_string();
    let old_tele = sanitize_telemetry_value(existing.get("recoveryTelemetry"), "unknown");
    let new_tele = sanitize_telemetry_value(payload.get("recoveryTelemetry"), &next_phase);

    let prior_phase = old_tele
        .get("currentPhase")
        .and_then(Value::as_str)
        .unwrap_or("unknown");

    let mut counters = Map::new();
    if let Some(old_c) = old_tele.get("counters").and_then(Value::as_object) {
        for (k, v) in old_c {
            if let Some(n) = v.as_i64() {
                if n > 0 {
                    counters.insert(k.clone(), json!(n));
                }
            }
        }
    }
    if let Some(new_c) = new_tele.get("counters").and_then(Value::as_object) {
        for (k, v) in new_c {
            if let Some(n) = v.as_i64() {
                let prev = counters.get(k).and_then(Value::as_i64).unwrap_or(0);
                let max_val = prev.max(n);
                if max_val > 0 {
                    counters.insert(k.clone(), json!(max_val));
                }
            }
        }
    }
    if prior_phase != next_phase {
        if next_phase == "paused" {
            let cur = counters
                .get("pauseOutcome")
                .and_then(Value::as_i64)
                .unwrap_or(0);
            counters.insert("pauseOutcome".into(), json!(cur + 1));
        }
        if next_phase == "aborted" || next_phase == "rolled-back" {
            let cur = counters
                .get("abortOutcome")
                .and_then(Value::as_i64)
                .unwrap_or(0);
            counters.insert("abortOutcome".into(), json!(cur + 1));
        }
    }
    let samples = new_tele
        .get("samples")
        .cloned()
        .unwrap_or_else(|| Value::Array(Vec::new()));
    payload.remove("_recoveryTelemetryPendingCounters");
    payload.insert(
        "recoveryTelemetry".into(),
        json!({
            "schemaVersion": 1,
            "counters": Value::Object(counters),
            "samples": samples,
            "currentPhase": next_phase,
        }),
    );
}

pub(crate) fn write_atomic_remote_session(
    path: &Path,
    payload: &mut Map<String, Value>,
    clear_pause: bool,
    clear_abort: bool,
    allow_control_transition: bool,
) -> Result<(), AppError> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|_| route_support::unhandled_error())?;
    }
    let lock_path = path.with_file_name(".remote-fetch.lock");
    let _lock =
        FileLockGuard::acquire(&lock_path, true).map_err(|_| route_support::unhandled_error())?;
    write_atomic_remote_session_unlocked(
        path,
        payload,
        clear_pause,
        clear_abort,
        allow_control_transition,
    )
}

fn write_atomic_remote_session_unlocked(
    path: &Path,
    payload: &mut Map<String, Value>,
    clear_pause: bool,
    clear_abort: bool,
    allow_control_transition: bool,
) -> Result<(), AppError> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|_| route_support::unhandled_error())?;
    }

    let existing: Map<String, Value> = if path.is_file() {
        fs::read_to_string(path)
            .ok()
            .and_then(|s| serde_json::from_str::<Value>(&s).ok())
            .and_then(|v| v.as_object().cloned())
            .unwrap_or_default()
    } else {
        Map::new()
    };

    if existing.get("pauseRequested").is_some_and(python_truthy) && !clear_pause {
        payload.insert("pauseRequested".into(), Value::Bool(true));
    }
    if existing.get("abortRequested").is_some_and(python_truthy) && !clear_abort {
        payload.insert("abortRequested".into(), Value::Bool(true));
    }
    let ex_gen = existing
        .get("controlGeneration")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let pl_gen = payload
        .get("controlGeneration")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    if ex_gen > pl_gen {
        payload.insert("controlGeneration".into(), json!(ex_gen));
        let ex_phase = existing
            .get("phase")
            .filter(|v| !v.is_null())
            .map(value_str)
            .unwrap_or_default();
        payload.insert("phase".into(), Value::String(ex_phase));
        for key in ["pauseRequested", "abortRequested", "pausedFromPhase"] {
            if let Some(val) = existing.get(key) {
                payload.insert(key.into(), val.clone());
            } else {
                payload.remove(key);
            }
        }
    }
    let existing_phase = existing
        .get("phase")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if is_controlled_stop_phase(existing_phase) && !(allow_control_transition && pl_gen >= ex_gen) {
        payload.insert("phase".into(), Value::String(existing_phase.to_string()));
        if let Some(pfp) = existing.get("pausedFromPhase") {
            if python_truthy(pfp) {
                payload.insert("pausedFromPhase".into(), pfp.clone());
            }
        }
    }

    update_telemetry_for_persist(&existing, payload);
    let body = serde_json::to_string_pretty(&Value::Object(payload.clone()))
        .map_err(|_| route_support::unhandled_error())?;
    let tmp = path.with_file_name(format!(
        ".remote-fetch.{}.{}.tmp",
        std::process::id(),
        format_uuid_like()?
    ));
    {
        let mut f = File::create(&tmp).map_err(|_| route_support::unhandled_error())?;
        f.write_all(body.as_bytes())
            .map_err(|_| route_support::unhandled_error())?;
        f.write_all(b"\n")
            .map_err(|_| route_support::unhandled_error())?;
        let _ = f.sync_all();
    }
    fs::rename(&tmp, path).map_err(|_| route_support::unhandled_error())?;
    Ok(())
}

pub(crate) fn release_session_holds(root: &Path, session: &Map<String, Value>) {
    let restore_id = session
        .get("restoreId")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if !restore_id.is_empty() {
        let owner_hash = hex_digest(&Sha256::digest(restore_id.as_bytes()));
        let pin_path = root
            .join(".backup-component-cache")
            .join("pins")
            .join(format!("{owner_hash}.json"));
        let _ = fs::remove_file(pin_path);
    }

    let target_id = session
        .get("targetId")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if target_id.is_empty() {
        return;
    }
    let mut keys: Vec<String> = session
        .get("holdKeys")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter(|v| !v.is_null())
                .map(value_str)
                .collect()
        })
        .unwrap_or_default();
    if keys.is_empty() {
        if let Some(hk) = session
            .get("holdKey")
            .filter(|v| python_truthy(v))
            .map(value_str)
        {
            if !hk.is_empty() {
                keys.push(hk);
            }
        }
    }
    if keys.is_empty() {
        return;
    }

    let base_dir: Option<PathBuf> = if target_id == "managed-local" {
        Some(root.join(".backups"))
    } else {
        let Ok(targets) = list_registered_targets() else {
            return;
        };
        let Some(target) = targets
            .into_iter()
            .find(|t| t.get("targetId").and_then(Value::as_str) == Some(target_id))
        else {
            return;
        };
        let kind = target.get("kind").and_then(Value::as_str).unwrap_or("");
        if kind == "filesystem" {
            target
                .get("path")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .map(PathBuf::from)
        } else {
            None
        }
    };
    if let Some(dir) = base_dir {
        for key in keys {
            let rel = key.trim_start_matches('/');
            if !rel.contains("..") {
                let candidate = dir.join(rel);
                if candidate.is_file() {
                    let _ = fs::remove_file(candidate);
                }
            }
        }
    }
}

fn scrub_remote_scratch(session_dir: &Path) {
    let Ok(entries) = fs::read_dir(session_dir) else {
        return;
    };
    for entry in entries.flatten() {
        let p = entry.path();
        if p.is_file() {
            let name = entry.file_name().to_string_lossy().into_owned();
            if name.starts_with("payload-decrypted-") && name.ends_with(".zip") {
                scrub_plaintext_file(&p);
            }
        }
    }
}

pub(crate) fn converge_job_control(
    root: &Path,
    restore_id: &str,
    session_dir: &Path,
    session_path: &Path,
    session: &mut Map<String, Value>,
) -> Result<String, AppError> {
    let tx_path = session_dir.join("transaction.json");
    let phase = session
        .get("phase")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();

    if session.get("abortRequested").is_some_and(python_truthy) {
        let abort_from = session
            .get("abortFromPhase")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .unwrap_or(&phase)
            .to_string();
        session.insert("phase".into(), Value::String("aborting".to_string()));
        scrub_remote_scratch(session_dir);
        let tx_phase = if !tx_path.is_file() {
            String::new()
        } else {
            fs::read_to_string(&tx_path)
                .ok()
                .and_then(|s| serde_json::from_str::<Value>(&s).ok())
                .and_then(|v| {
                    v.as_object()
                        .and_then(|o| o.get("phase"))
                        .and_then(Value::as_str)
                        .map(str::to_string)
                })
                .unwrap_or_else(|| "recovery-required".to_string())
        };
        if is_uncertain_tx_phase(&tx_phase)
            || abort_from == "committing"
            || abort_from == "recovery-required"
        {
            session.insert(
                "phase".into(),
                Value::String("recovery-required".to_string()),
            );
            session.insert("updatedAt".into(), Value::String(now_utc_iso()));
            write_atomic_remote_session_unlocked(session_path, session, false, false, true)?;
            return Ok("recovery-required".to_string());
        }
        let next_phase = if !tx_phase.is_empty() {
            abort_restore(root, restore_id)?;
            "rolled-back".to_string()
        } else {
            "aborted".to_string()
        };
        session.insert("phase".into(), Value::String(next_phase.clone()));
        session.remove("abortRequested");
        session.remove("pauseRequested");
        session.remove("abortFromPhase");
        release_session_holds(root, session);
        session.insert("updatedAt".into(), Value::String(now_utc_iso()));
        write_atomic_remote_session_unlocked(session_path, session, true, true, true)?;
        return Ok(next_phase);
    }

    if session.get("pauseRequested").is_some_and(python_truthy) && phase != "paused" {
        session.insert("pausedFromPhase".into(), Value::String(phase));
        session.insert("phase".into(), Value::String("paused".to_string()));
        session.insert("updatedAt".into(), Value::String(now_utc_iso()));
        write_atomic_remote_session_unlocked(session_path, session, false, false, true)?;
        return Ok("paused".to_string());
    }

    session.insert("updatedAt".into(), Value::String(now_utc_iso()));
    write_atomic_remote_session_unlocked(session_path, session, false, false, true)?;
    Ok(phase)
}

pub(crate) fn advance_federated_phase(
    root: &Path,
    restore_id: &str,
    phase: &str,
) -> Result<(), AppError> {
    safe_restore_session_id(restore_id)?;
    let path = remote_session_path(root, restore_id)?;
    let Some(mut session) = read_remote_restore_session(root, restore_id)? else {
        return Ok(());
    };
    session.insert("phase".into(), Value::String(phase.to_string()));
    session.insert("updatedAt".into(), Value::String(now_utc_iso()));
    write_atomic_remote_session(&path, &mut session, false, false, false)?;
    if is_terminal_remote_phase(phase) {
        release_session_holds(root, &session);
    }
    Ok(())
}

pub(crate) fn request_restore_pause(root: &Path, restore_id: &str) -> Result<Value, AppError> {
    let path = remote_session_path(root, restore_id)?;
    let session_dir = path
        .parent()
        .ok_or_else(route_support::unhandled_error)?
        .to_path_buf();
    let lock_path = path.with_file_name(".remote-fetch.lock");
    let _lock =
        FileLockGuard::acquire(&lock_path, true).map_err(|_| route_support::unhandled_error())?;
    let mut session = read_remote_restore_session(root, restore_id)?
        .ok_or_else(|| AppError::not_found("Restore session not found"))?;
    let current_phase = session
        .get("phase")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if is_terminal_remote_phase(current_phase) {
        return Err(AppError {
            message: "Terminal recovery job cannot be paused".to_string(),
            code: codes::INVALID_REQUEST,
            status: 409,
        });
    }
    if current_phase == "paused" {
        return Ok(Value::Object(session));
    }
    let generation = session
        .get("controlGeneration")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    session.insert("controlGeneration".into(), json!(generation + 1));
    session.insert("pauseRequested".into(), Value::Bool(true));
    converge_job_control(root, restore_id, &session_dir, &path, &mut session)?;
    Ok(Value::Object(session))
}

pub(crate) fn resume_restore_session(root: &Path, restore_id: &str) -> Result<Value, AppError> {
    let path = remote_session_path(root, restore_id)?;
    let lock_path = path.with_file_name(".remote-fetch.lock");
    let _lock =
        FileLockGuard::acquire(&lock_path, true).map_err(|_| route_support::unhandled_error())?;
    let mut session = read_remote_restore_session(root, restore_id)?
        .ok_or_else(|| AppError::not_found("Restore session not found"))?;
    let current_phase = session
        .get("phase")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if current_phase != "paused" {
        return Ok(Value::Object(session));
    }
    let target_phase = session
        .get("pausedFromPhase")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .unwrap_or("fetching-selected-components")
        .to_string();
    let generation = session
        .get("controlGeneration")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    session.insert("controlGeneration".into(), json!(generation + 1));
    session.insert("phase".into(), Value::String(target_phase));
    session.remove("pauseRequested");
    session.remove("pausedFromPhase");
    session.insert("updatedAt".into(), Value::String(now_utc_iso()));
    write_atomic_remote_session_unlocked(&path, &mut session, true, false, true)?;
    Ok(Value::Object(session))
}

pub(crate) fn request_restore_abort(root: &Path, restore_id: &str) -> Result<Value, AppError> {
    let path = remote_session_path(root, restore_id)?;
    let session_dir = path
        .parent()
        .ok_or_else(route_support::unhandled_error)?
        .to_path_buf();
    let lock_path = path.with_file_name(".remote-fetch.lock");
    let _lock =
        FileLockGuard::acquire(&lock_path, true).map_err(|_| route_support::unhandled_error())?;
    let mut session = read_remote_restore_session(root, restore_id)?
        .ok_or_else(|| AppError::not_found("Restore session not found"))?;
    let current_phase = session
        .get("phase")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    if is_terminal_remote_phase(&current_phase) {
        return Err(AppError {
            message: "Terminal recovery job cannot be aborted".to_string(),
            code: codes::INVALID_REQUEST,
            status: 409,
        });
    }
    let generation = session
        .get("controlGeneration")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    session.insert("controlGeneration".into(), json!(generation + 1));
    let abort_from = session
        .get("abortFromPhase")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .unwrap_or(current_phase);
    session.insert("abortFromPhase".into(), Value::String(abort_from));
    session.insert("abortRequested".into(), Value::Bool(true));
    converge_job_control(root, restore_id, &session_dir, &path, &mut session)?;
    Ok(Value::Object(session))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rewrite_typed_json_references_remaps_ids_and_paths_only() {
        let mut map = BTreeMap::new();
        map.insert("proj1".to_string(), "proj1-imported-abc".to_string());
        let fields = contributor_fields("projects");
        let input = serde_json::to_vec(&json!({
            "id": "proj1",
            "sourcePath": "proj1/notes.txt",
            "title": "proj1 freeform title"
        }))
        .unwrap();
        let out = rewrite_typed_json_references(&fields, &input, &map, "backup_1");
        let decoded: Value = serde_json::from_slice(&out).unwrap();
        assert_eq!(decoded["id"], "proj1-imported-abc");
        assert_eq!(decoded["sourcePath"], "proj1-imported-abc/notes.txt");
        assert_eq!(decoded["title"], "proj1 freeform title");
        assert_eq!(decoded["importedFrom"]["originalId"], "proj1");
        assert_eq!(decoded["importedFrom"]["sourceBackupId"], "backup_1");
    }

    #[test]
    fn merge_json_payload_deduplicates_arrays_and_recursively_merges_objects() {
        let existing = serde_json::to_vec(&json!({
            "items": [{"id": "a"}, {"id": "b"}],
            "meta": {"keep": 1}
        }))
        .unwrap();
        let incoming = serde_json::to_vec(&json!({
            "items": [{"id": "b"}, {"id": "c"}],
            "meta": {"keep": 9, "added": 2}
        }))
        .unwrap();
        let merged_bytes = merge_json_payload(&existing, &incoming);
        let merged: Value = serde_json::from_slice(&merged_bytes).unwrap();
        assert_eq!(merged["items"].as_array().unwrap().len(), 3);
        assert_eq!(merged["meta"]["keep"], 1);
        assert_eq!(merged["meta"]["added"], 2);
    }

    #[test]
    fn stem_and_suffix_matches_python_path_semantics() {
        assert_eq!(stem_and_suffix("file.json"), ("file", ".json"));
        assert_eq!(stem_and_suffix(".projects"), (".projects", ""));
        assert_eq!(stem_and_suffix("a.b.txt"), ("a.b", ".txt"));
        assert_eq!(stem_and_suffix(""), ("", ""));
    }
}
