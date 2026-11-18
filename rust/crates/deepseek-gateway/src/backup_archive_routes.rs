//! Native S5 backup archive finalization, inspection, and unlock routes under `restore_staging_store`:
//! - `POST /api/workspace/backups/{backup_id}/finalize` (`finalize_session`)
//! - `POST /api/workspace/restores/inspect` (`inspect_archive`)
//! - `POST /api/workspace/restores/{restore_id}/unlock` (`unlock_restore`)
//!
//! Write authority follows `DECLARED_NATIVE_DATA_DOMAINS`: all three routes
//! require `DEEPSEEK_RUNTIME_MODE=python_disabled` (`restore_staging_store`) and
//! fail closed with `409 NATIVE_RESTORE_STAGING_STORE_WRITE_NOT_OWNED` otherwise.

use std::collections::BTreeSet;
use std::fs::{self, File, OpenOptions};
use std::io::{BufReader, BufWriter, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use axum::Router;
use axum::body::{Body, Bytes};
use axum::extract::{DefaultBodyLimit, Path as AxumPath};
use axum::http::{HeaderMap, Method};
use axum::response::Response;
use axum::routing::any;
use deepseek_core::version_info;
use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::backup_mirror::{fsync_dir, sha256_file};
use deepseek_policy::backup_unlock::{
    BACKUP_SCHEMA, PACKAGE_PURPOSE, decrypt_file_native, decrypt_passphrase_file_native,
    derive_recipients, encrypt_tree_zip_native, inspect_age_file_protection,
    safe_extract_and_verify, scrub_plaintext_file, unlock_sealed_frontend,
};
use deepseek_policy::core_utils::python_truthy;
use deepseek_policy::entropy::{Entropy, SystemEntropy};
use deepseek_policy::mutation_gate;
use deepseek_policy::python_json::dumps_compact;
use futures_util::StreamExt;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use crate::backup_capacity_routes::now_utc_iso;
use crate::backup_restore_read_routes::{
    load_session, now_unix_secs_f64, public_session, read_json_object, restore_root,
    validate_backup_id, validate_restore_id,
};
use crate::backup_restore_write_routes::{
    BackupContext, ContributorSpec, LOCAL_CONTRIBUTORS, collect_contributor_files, excluded_path,
    finish_json, parse_context, require_write_authority, validate_frontend_envelope,
    write_atomic_json,
};
use crate::backup_secret_slots::{
    SecretBytes, consume_secret, put_secret_bytes, record_unlock_failure,
};
use crate::file_text_route::parse_first_backup_multipart;
use crate::route_support;

pub const MAX_ARCHIVE_BYTES: u64 = 2_000_000_000;
const MAX_ARCHIVE_MULTIPART_BUFFER: usize = 220_000_000;
const FINALIZE_MAX_RETRIES: u32 = 3;

static TEMP_ARCHIVE_SEQ: AtomicU64 = AtomicU64::new(0);

pub fn router() -> Router {
    Router::new()
        .route(
            "/api/workspace/backups/:backup_id/finalize",
            any(dispatch_finalize_backup),
        )
        .route(
            "/api/workspace/restores/inspect",
            any(dispatch_inspect_restore).layer(DefaultBodyLimit::disable()),
        )
        .route(
            "/api/workspace/restores/:restore_id/unlock",
            any(dispatch_unlock_restore),
        )
}

async fn dispatch_finalize_backup(
    method: Method,
    AxumPath(backup_id): AxumPath<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if method != Method::POST {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    if let Err(err) = require_write_authority() {
        return route_support::app_error_response(err);
    }
    if !body.is_empty() {
        if let Err(err) = route_support::read_json_object(&headers, &body, 64_000) {
            return route_support::app_error_response(err);
        }
    }
    let joined = tokio::task::spawn_blocking(move || {
        let root = crate::data_routes::workspace_root();
        finalize_session(&root, &backup_id)
    })
    .await;
    finish_json(joined)
}

async fn dispatch_inspect_restore(method: Method, headers: HeaderMap, body: Body) -> Response {
    if method != Method::POST {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    if let Err(err) = require_write_authority() {
        return route_support::app_error_response(err);
    }
    if let Some(raw_len) = headers.get(axum::http::header::CONTENT_LENGTH) {
        let parsed = raw_len
            .to_str()
            .ok()
            .and_then(|s| s.trim().parse::<u64>().ok());
        match parsed {
            Some(len) if len > MAX_ARCHIVE_BYTES => {
                return route_support::app_error_response(AppError {
                    message: "Backup archive is too large".to_string(),
                    code: codes::UPLOAD_TOO_LARGE,
                    status: 413,
                });
            }
            None => {
                return route_support::app_error_response(AppError::invalid_payload(
                    "Invalid Content-Length",
                ));
            }
            Some(_) => {}
        }
    }

    let content_type = headers
        .get(axum::http::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_string();
    let header_filename = headers
        .get("x-backup-filename")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("workspace.dsibackup")
        .trim()
        .to_string();

    let root = crate::data_routes::workspace_root();
    let staged = match stage_inspect_upload(&root, &content_type, &header_filename, body).await {
        Ok(staged) => staged,
        Err(err) => return route_support::app_error_response(err),
    };

    let joined = tokio::task::spawn_blocking(move || {
        let _guard = FileCleanupGuard::new(staged.path.clone());
        inspect_staged_archive(&root, &staged.path, &staged.filename, &staged.sha256)
    })
    .await;
    finish_json(joined)
}

async fn dispatch_unlock_restore(
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
        unlock_restore(&root, &restore_id)
    })
    .await;
    finish_json(joined)
}

struct StagedUpload {
    path: PathBuf,
    filename: String,
    sha256: String,
}

struct DirCleanupGuard {
    path: PathBuf,
    armed: bool,
}

impl DirCleanupGuard {
    fn new(path: PathBuf) -> Self {
        Self { path, armed: true }
    }

    fn disarm(&mut self) {
        self.armed = false;
    }
}

impl Drop for DirCleanupGuard {
    fn drop(&mut self) {
        if self.armed {
            let _ = fs::remove_dir_all(&self.path);
        }
    }
}

struct FileCleanupGuard {
    path: PathBuf,
    armed: bool,
}

impl FileCleanupGuard {
    fn new(path: PathBuf) -> Self {
        Self { path, armed: true }
    }

    fn disarm(&mut self) {
        self.armed = false;
    }
}

impl Drop for FileCleanupGuard {
    fn drop(&mut self) {
        if self.armed {
            scrub_plaintext_file(&self.path);
        }
    }
}

struct ExternalSnapshotGuard {
    base_url: String,
    backup_id: String,
    token: String,
}

impl Drop for ExternalSnapshotGuard {
    fn drop(&mut self) {
        let url = format!(
            "{}/internal/backups/{}/release",
            self.base_url.trim_end_matches('/'),
            self.backup_id
        );
        if let Ok(client) = reqwest::blocking::Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(Duration::from_secs(5))
            .build()
        {
            let mut req = client.post(url).body("{}");
            if !self.token.is_empty() {
                req = req.bearer_auth(&self.token);
            }
            let _ = req.send();
        }
    }
}

async fn stage_inspect_upload(
    root: &Path,
    content_type: &str,
    header_filename: &str,
    body: Body,
) -> Result<StagedUpload, AppError> {
    if content_type
        .to_ascii_lowercase()
        .starts_with("multipart/form-data")
    {
        let mut raw = Vec::new();
        let mut stream = Box::pin(body.into_data_stream());
        while let Some(next) = stream.next().await {
            let chunk = next.map_err(|_| AppError::invalid_payload("Backup upload failed"))?;
            if raw.len().saturating_add(chunk.len()) > MAX_ARCHIVE_MULTIPART_BUFFER {
                return Err(AppError {
                    message: "Backup archive is too large".to_string(),
                    code: codes::UPLOAD_TOO_LARGE,
                    status: 413,
                });
            }
            raw.extend_from_slice(&chunk);
        }
        let max_part = usize::try_from(MAX_ARCHIVE_BYTES).unwrap_or(MAX_ARCHIVE_MULTIPART_BUFFER);
        let (part_name, data) = parse_first_backup_multipart(content_type, &raw, max_part)?;
        if data.is_empty() {
            return Err(AppError::invalid_payload("Backup archive is empty"));
        }
        let upload_dir = root.join(".restore-staging").join(".uploads");
        fs::create_dir_all(&upload_dir).map_err(|_| route_support::unhandled_error())?;
        let seq = TEMP_ARCHIVE_SEQ.fetch_add(1, Ordering::Relaxed);
        let temp_path = upload_dir.join(format!("upload-{}-{seq}.part", std::process::id()));
        let mut guard = FileCleanupGuard::new(temp_path.clone());
        let mut hasher = Sha256::new();
        hasher.update(&data);
        let digest = hex_digest(&hasher.finalize());
        {
            let mut file = OpenOptions::new()
                .write(true)
                .create(true)
                .truncate(true)
                .open(&temp_path)
                .map_err(|_| route_support::unhandled_error())?;
            file.write_all(&data)
                .and_then(|()| file.flush())
                .map_err(|_| route_support::unhandled_error())?;
        }
        guard.disarm();
        let chosen_name = if part_name.is_empty() {
            if header_filename.is_empty() {
                "workspace.dsibackup".to_string()
            } else {
                header_filename.to_string()
            }
        } else {
            part_name
        };
        return Ok(StagedUpload {
            path: temp_path,
            filename: chosen_name,
            sha256: digest,
        });
    }

    let upload_dir = root.join(".restore-staging").join(".uploads");
    fs::create_dir_all(&upload_dir).map_err(|_| route_support::unhandled_error())?;
    let seq = TEMP_ARCHIVE_SEQ.fetch_add(1, Ordering::Relaxed);
    let temp_path = upload_dir.join(format!("upload-{}-{seq}.part", std::process::id()));
    let mut guard = FileCleanupGuard::new(temp_path.clone());
    let mut writer = BufWriter::new(
        OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .open(&temp_path)
            .map_err(|_| route_support::unhandled_error())?,
    );
    let mut hasher = Sha256::new();
    let mut total: u64 = 0;
    let mut stream = Box::pin(body.into_data_stream());
    while let Some(next) = stream.next().await {
        let chunk = next.map_err(|_| AppError::invalid_payload("Backup upload failed"))?;
        total = total.saturating_add(chunk.len() as u64);
        if total > MAX_ARCHIVE_BYTES {
            drop(writer);
            drop(guard);
            return Err(AppError {
                message: "Backup archive is too large".to_string(),
                code: codes::UPLOAD_TOO_LARGE,
                status: 413,
            });
        }
        hasher.update(&chunk);
        writer
            .write_all(&chunk)
            .map_err(|_| route_support::unhandled_error())?;
    }
    writer
        .flush()
        .map_err(|_| route_support::unhandled_error())?;
    drop(writer);
    if total == 0 {
        drop(guard);
        return Err(AppError::invalid_payload("Backup archive is empty"));
    }
    guard.disarm();
    let chosen_name = if header_filename.is_empty() {
        "workspace.dsibackup".to_string()
    } else {
        header_filename.to_string()
    };
    Ok(StagedUpload {
        path: temp_path,
        filename: chosen_name,
        sha256: hex_digest(&hasher.finalize()),
    })
}

pub(crate) fn gate_to_app_error(err: deepseek_policy::mutation_gate::GateError) -> AppError {
    AppError {
        message: err.message,
        code: err.code.unwrap_or(codes::INVALID_REQUEST),
        status: err.status.unwrap_or(423),
    }
}

pub(crate) fn hex_digest(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push_str(&format!("{byte:02x}"));
    }
    out
}

fn context_from_object(raw: Option<&Value>) -> Result<BackupContext, AppError> {
    let map = raw.and_then(Value::as_object).cloned().unwrap_or_default();
    parse_context(&map)
}

fn finalize_session(root: &Path, backup_id: &str) -> Result<Value, AppError> {
    finalize_session_with_owner(root, backup_id, None)
}

pub(crate) fn finalize_session_with_owner(
    root: &Path,
    backup_id: &str,
    owner_restore_id: Option<&str>,
) -> Result<Value, AppError> {
    validate_backup_id(backup_id)?;
    mutation_gate::assert_mutation_allowed(owner_restore_id, root).map_err(gate_to_app_error)?;

    let mut session = load_session(root, backup_id)?;
    if session.get("phase").and_then(Value::as_str) == Some("ready") {
        return Ok(public_session(session));
    }

    let session_dir = root.join(".backups").join("sessions").join(backup_id);
    let requires_frontend = session
        .get("requiresFrontendState")
        .map(python_truthy)
        .unwrap_or(true);
    let frontend_path = session_dir.join("frontend.json");
    let frontend_envelope = if frontend_path.is_file() {
        let obj = read_json_object(&frontend_path)?;
        validate_frontend_envelope(&obj)?;
        Some(obj)
    } else {
        None
    };
    if requires_frontend && frontend_envelope.is_none() {
        return Err(AppError {
            message: "Verified frontend state is required".to_string(),
            code: codes::INVALID_REQUEST,
            status: 409,
        });
    }

    let context = context_from_object(session.get("context"))?;
    let complete = session
        .get("coverage")
        .and_then(|c| c.get("complete"))
        .map(python_truthy)
        .unwrap_or(true);
    if !complete && context.coverage_policy == "strict" {
        return Err(AppError {
            message: "Strict backup coverage failed".to_string(),
            code: codes::INVALID_REQUEST,
            status: 409,
        });
    }

    let protection = session
        .get("protection")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_else(|| {
            let mut m = Map::new();
            m.insert("mode".into(), Value::String("none".to_string()));
            m
        });
    let protection_mode = protection
        .get("mode")
        .and_then(Value::as_str)
        .unwrap_or("none")
        .to_string();
    let recipients: Vec<String> = protection
        .get("recipients")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(|v| v.as_str().map(str::to_string))
                .collect()
        })
        .unwrap_or_default();

    let secret_guard: Option<(String, SecretBytes)> = if protection_mode != "none" {
        let expected_kind = if protection_mode == "passphrase" {
            "passphrase"
        } else {
            "age-identity"
        };
        let (kind, secret) = consume_secret(backup_id, Some(expected_kind))?;
        if protection_mode == "age-recipient" {
            let derived = derive_recipients(secret.as_str()?)?;
            if !derived
                .iter()
                .any(|cand| recipients.iter().any(|allowed| allowed == cand))
            {
                return Err(AppError::invalid_payload(
                    "Recovery key does not match backup recipient",
                ));
            }
        }
        Some((kind, secret))
    } else {
        None
    };

    let mut last_error: Option<AppError> = None;
    for attempt in 1..=FINALIZE_MAX_RETRIES {
        let start_gen = mutation_gate::read_generation(root);
        let built = build_archive(
            root,
            backup_id,
            &session,
            &context,
            frontend_envelope.as_ref(),
            &protection_mode,
            secret_guard.as_ref().map(|(_, s)| s),
            &recipients,
        );
        match built {
            Ok(outcome) => {
                let end_gen = mutation_gate::read_generation(root);
                if start_gen == end_gen {
                    session.insert("phase".into(), Value::String("ready".to_string()));
                    session.insert("filename".into(), Value::String(outcome.filename));
                    session.insert(
                        "path".into(),
                        Value::String(outcome.path.to_string_lossy().into_owned()),
                    );
                    session.insert("size".into(), json!(outcome.size));
                    session.insert(
                        "manifestDigest".into(),
                        Value::String(outcome.manifest_digest),
                    );
                    session.insert("protection".into(), json!({ "mode": protection_mode }));
                    session.insert(
                        "downloadUrl".into(),
                        Value::String(format!("/api/workspace/backups/{backup_id}/download")),
                    );
                    session.insert("snapshotAttempt".into(), json!(attempt));
                    write_atomic_json(
                        &session_dir.join("session.json"),
                        &Value::Object(session.clone()),
                    )?;
                    return Ok(public_session(session));
                }
                let _ = fs::remove_file(&outcome.path);
            }
            Err(err) => {
                session.insert("phase".into(), Value::String("failed".to_string()));
                session.insert("error".into(), Value::String(err.message.clone()));
                let _ =
                    write_atomic_json(&session_dir.join("session.json"), &Value::Object(session));
                return Err(err);
            }
        }
        last_error = Some(AppError {
            message: "Workspace data changed repeatedly during backup; try again".to_string(),
            code: codes::INVALID_REQUEST,
            status: 409,
        });
    }

    session.insert("phase".into(), Value::String("failed".to_string()));
    if let Some(ref err) = last_error {
        session.insert("error".into(), Value::String(err.message.clone()));
    }
    let _ = write_atomic_json(&session_dir.join("session.json"), &Value::Object(session));
    Err(last_error.unwrap_or_else(route_support::unhandled_error))
}

struct BuildArchiveOutcome {
    filename: String,
    path: PathBuf,
    size: u64,
    manifest_digest: String,
}

fn copy_sqlite_consistent(src: &Path, dst: &Path) -> Result<(), AppError> {
    if let Some(parent) = dst.parent() {
        fs::create_dir_all(parent).map_err(|_| route_support::unhandled_error())?;
    }
    if dst.exists() {
        let _ = fs::remove_file(dst);
    }
    let flags =
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX;
    let src_conn = rusqlite::Connection::open_with_flags(src, flags).map_err(|err| {
        AppError::invalid_payload(format!("Failed to take consistent SQLite snapshot: {err}"))
    })?;
    let dst_str = dst.to_string_lossy().into_owned();
    src_conn
        .execute("VACUUM INTO ?1", [&dst_str])
        .map_err(|err| {
            let _ = fs::remove_file(dst);
            AppError::invalid_payload(format!("Failed to take consistent SQLite snapshot: {err}"))
        })?;
    Ok(())
}

fn is_sqlite_path(path: &Path) -> bool {
    path.extension()
        .and_then(|ext| ext.to_str())
        .map(|ext| ext.to_ascii_lowercase())
        .is_some_and(|ext| matches!(ext.as_str(), "sqlite3" | "sqlite" | "db"))
}

fn copy_staged_file(src: &Path, dst: &Path) -> Result<(), AppError> {
    if let Some(parent) = dst.parent() {
        fs::create_dir_all(parent).map_err(|_| route_support::unhandled_error())?;
    }
    if is_sqlite_path(src) {
        if src
            .extension()
            .and_then(|e| e.to_str())
            .is_some_and(|e| e.eq_ignore_ascii_case("sqlite3"))
        {
            return copy_sqlite_consistent(src, dst);
        }
        // Other `.sqlite` / `.db` files may or may not contain SQLite headers if a fixture wrote placeholder text.
        if let Ok(mut file) = File::open(src) {
            let mut magic = [0u8; 16];
            if file.read_exact(&mut magic).is_ok() && &magic == b"SQLite format 3\0" {
                return copy_sqlite_consistent(src, dst);
            }
        }
    }
    fs::copy(src, dst).map_err(|_| route_support::unhandled_error())?;
    Ok(())
}

fn snapshot_external_contributor(
    staging: &Path,
    backup_id: &str,
) -> Result<(Value, Value), AppError> {
    let base_url = std::env::var("STATELESS_MCP_BACKUP_URL")
        .unwrap_or_default()
        .trim()
        .to_string();
    if base_url.is_empty() {
        return Err(AppError {
            message: "Stateless MCP backup contributor is unavailable".to_string(),
            code: codes::INVALID_REQUEST,
            status: 409,
        });
    }
    let token = std::env::var("STATELESS_MCP_BACKUP_TOKEN")
        .unwrap_or_default()
        .trim()
        .to_string();
    let client = reqwest::blocking::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(60))
        .build()
        .map_err(|_| route_support::unhandled_error())?;

    let snap_url = format!(
        "{}/internal/backups/{backup_id}/snapshot",
        base_url.trim_end_matches('/')
    );
    let mut snap_req = client.post(snap_url).body("{}");
    if !token.is_empty() {
        snap_req = snap_req.bearer_auth(&token);
    }
    let snap_resp = snap_req
        .send()
        .map_err(|_| AppError::invalid_payload("Stateless MCP snapshot failed"))?;
    if !snap_resp.status().is_success() {
        return Err(AppError::invalid_payload("Stateless MCP snapshot failed"));
    }
    let _guard = ExternalSnapshotGuard {
        base_url: base_url.clone(),
        backup_id: backup_id.to_string(),
        token: token.clone(),
    };

    let stream_url = format!(
        "{}/internal/backups/{backup_id}/stream",
        base_url.trim_end_matches('/')
    );
    let mut stream_req = client.get(stream_url);
    if !token.is_empty() {
        stream_req = stream_req.bearer_auth(&token);
    }
    let mut stream_resp = stream_req
        .send()
        .map_err(|_| AppError::invalid_payload("Stateless MCP stream failed"))?;
    if !stream_resp.status().is_success() {
        return Err(AppError::invalid_payload("Stateless MCP stream failed"));
    }
    let target = staging
        .join("payload")
        .join("stateless-mcp")
        .join("state.jsonl");
    if let Some(parent) = target.parent() {
        fs::create_dir_all(parent).map_err(|_| route_support::unhandled_error())?;
    }
    let mut out = BufWriter::new(
        OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .open(&target)
            .map_err(|_| route_support::unhandled_error())?,
    );
    let mut hasher = Sha256::new();
    let mut size: u64 = 0;
    let mut buf = [0u8; 64 * 1024];
    loop {
        let count = stream_resp
            .read(&mut buf)
            .map_err(|_| AppError::invalid_payload("Stateless MCP stream failed"))?;
        if count == 0 {
            break;
        }
        hasher.update(&buf[..count]);
        size = size.saturating_add(count as u64);
        out.write_all(&buf[..count])
            .map_err(|_| route_support::unhandled_error())?;
    }
    out.flush().map_err(|_| route_support::unhandled_error())?;
    drop(out);

    let sha = hex_digest(&hasher.finalize());
    let text = fs::read_to_string(&target)
        .map_err(|_| AppError::invalid_payload("stateless-mcp: snapshot is invalid"))?;
    let mut records = 0usize;
    let mut complete = false;
    for line in text.lines() {
        let parsed: Value = serde_json::from_str(line)
            .map_err(|_| AppError::invalid_payload("stateless-mcp: snapshot is invalid"))?;
        if parsed.get("schemaVersion").and_then(Value::as_i64) != Some(1) {
            return Err(AppError::invalid_payload(
                "stateless-mcp: unsupported snapshot schema",
            ));
        }
        if parsed.get("type").and_then(Value::as_str) == Some("complete") {
            complete = true;
        }
        records += 1;
    }
    if !complete {
        return Err(AppError::invalid_payload(
            "stateless-mcp: snapshot is incomplete",
        ));
    }

    let entry = json!({
        "contributorId": "stateless-mcp",
        "path": "payload/stateless-mcp/state.jsonl",
        "size": size,
        "sha256": sha,
    });
    let mut c_hasher = Sha256::new();
    c_hasher.update(dumps_compact(&entry).as_bytes());
    let summary = json!({
        "id": "stateless-mcp",
        "schemaVersion": 1,
        "records": records,
        "bytes": size,
        "digest": hex_digest(&c_hasher.finalize()),
        "restorePolicy": "merge",
    });
    Ok((summary, entry))
}

fn build_revision() -> String {
    if let Ok(val) = std::env::var("DEEPSEEK_BUILD_REVISION") {
        let trimmed = val.trim();
        if !trimmed.is_empty() {
            return trimmed.to_string();
        }
    }
    if let Ok(val) = std::env::var("GITHUB_SHA") {
        let trimmed = val.trim();
        if !trimmed.is_empty() {
            return trimmed.to_string();
        }
    }
    "unknown".to_string()
}

fn current_platform_label() -> String {
    format!("{}-{}", std::env::consts::OS, std::env::consts::ARCH)
}

#[allow(clippy::too_many_arguments)]
fn build_archive(
    root: &Path,
    backup_id: &str,
    session: &Map<String, Value>,
    context: &BackupContext,
    frontend_envelope: Option<&Map<String, Value>>,
    protection_mode: &str,
    secret: Option<&SecretBytes>,
    recipients: &[String],
) -> Result<BuildArchiveOutcome, AppError> {
    let encrypted = protection_mode != "none";
    let backup_dir = root.join(".backups");
    fs::create_dir_all(&backup_dir).map_err(|_| route_support::unhandled_error())?;

    let seq = TEMP_ARCHIVE_SEQ.fetch_add(1, Ordering::Relaxed);
    let pid = std::process::id();
    let staging = backup_dir.join(format!(".staging-{backup_id}-{pid}-{seq}"));
    let verify_dir = backup_dir.join(format!(".verify-{backup_id}-{pid}-{seq}"));
    let verify_decrypted = backup_dir.join(format!(".verify-{backup_id}-{pid}-{seq}.dsibackup"));
    fs::create_dir_all(&staging).map_err(|_| route_support::unhandled_error())?;
    let _staging_guard = DirCleanupGuard::new(staging.clone());
    let _verify_dir_guard = DirCleanupGuard::new(verify_dir.clone());
    let _verify_dec_guard = FileCleanupGuard::new(verify_decrypted.clone());

    let selected_ids: BTreeSet<String> = session
        .get("contributorPlan")
        .and_then(|p| p.get("contributors"))
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter(|item| item.get("status").and_then(Value::as_str) == Some("selected"))
                .filter_map(|item| item.get("id").and_then(Value::as_str).map(str::to_string))
                .collect()
        })
        .unwrap_or_default();

    let mut manifest_files: Vec<Value> = Vec::new();
    let mut contributor_summaries: Vec<Value> = Vec::new();
    let mut source_schemas = Map::new();

    for spec in &LOCAL_CONTRIBUTORS {
        if !selected_ids.contains(spec.id) {
            continue;
        }
        source_schemas.insert(spec.id.to_string(), json!(spec.schema_version));
        let files = collect_contributor_files(root, spec, context);
        let mut c_bytes: u64 = 0;
        let mut c_records: usize = 0;
        let mut c_hasher = Sha256::new();
        for (rel, src_path) in files {
            let mut rel_path = staging.join("payload").join(spec.id);
            for part in rel.split('/') {
                rel_path.push(part);
            }
            copy_staged_file(&src_path, &rel_path)?;
            let size = fs::metadata(&rel_path)
                .map(|m| m.len())
                .map_err(|_| route_support::unhandled_error())?;
            let sha = sha256_file(&rel_path).map_err(|_| route_support::unhandled_error())?;
            let archive_path = format!("payload/{}/{rel}", spec.id);
            let entry = json!({
                "contributorId": spec.id,
                "path": archive_path,
                "size": size,
                "sha256": sha,
            });
            c_hasher.update(dumps_compact(&entry).as_bytes());
            manifest_files.push(entry);
            c_bytes = c_bytes.saturating_add(size);
            c_records += 1;
        }
        contributor_summaries.push(json!({
            "id": spec.id,
            "schemaVersion": spec.schema_version,
            "records": c_records,
            "bytes": c_bytes,
            "digest": hex_digest(&c_hasher.finalize()),
            "restorePolicy": "merge",
        }));
    }

    if selected_ids.contains("stateless-mcp") {
        let (summary, entry) = snapshot_external_contributor(&staging, backup_id)?;
        source_schemas.insert("stateless-mcp".to_string(), json!(1));
        contributor_summaries.push(summary);
        manifest_files.push(entry);
    }

    let mut frontend_meta: Option<Value> = None;
    if let Some(fe) = frontend_envelope {
        let fe_dir = staging.join("frontend");
        fs::create_dir_all(&fe_dir).map_err(|_| route_support::unhandled_error())?;
        let fe_path = fe_dir.join("state.json");
        let encoded = dumps_compact(&Value::Object(fe.clone()));
        fs::write(&fe_path, encoded.as_bytes()).map_err(|_| route_support::unhandled_error())?;
        let sha = sha256_file(&fe_path).map_err(|_| route_support::unhandled_error())?;
        manifest_files.push(json!({
            "contributorId": "frontend",
            "path": "frontend/state.json",
            "size": encoded.len(),
            "sha256": sha,
        }));
        let convs = fe
            .get("conversations")
            .and_then(Value::as_array)
            .map(|a| a.len())
            .unwrap_or(0);
        let conflicts = fe
            .get("conflicts")
            .and_then(Value::as_array)
            .map(|a| a.len())
            .unwrap_or(0);
        frontend_meta = Some(json!({
            "schemaVersion": 1,
            "digest": fe.get("digest").cloned().unwrap_or(Value::Null),
            "conversations": convs,
            "conflicts": conflicts,
        }));
    }

    let migration_dir = staging.join("migration");
    fs::create_dir_all(&migration_dir).map_err(|_| route_support::unhandled_error())?;
    let schemas_path = migration_dir.join("source-schemas.json");
    let schemas_encoded = dumps_compact(&Value::Object(source_schemas));
    fs::write(&schemas_path, schemas_encoded.as_bytes())
        .map_err(|_| route_support::unhandled_error())?;
    let schemas_sha = sha256_file(&schemas_path).map_err(|_| route_support::unhandled_error())?;
    manifest_files.push(json!({
        "contributorId": "migration",
        "path": "migration/source-schemas.json",
        "size": schemas_encoded.len(),
        "sha256": schemas_sha,
    }));

    manifest_files.sort_by(|left, right| {
        let lp = left.get("path").and_then(Value::as_str).unwrap_or("");
        let rp = right.get("path").and_then(Value::as_str).unwrap_or("");
        lp.cmp(rp)
    });

    let created_iso = now_utc_iso();
    let app_version = version_info().version.to_string();
    let mut manifest_obj = Map::new();
    manifest_obj.insert(
        "schemaVersion".into(),
        Value::String(BACKUP_SCHEMA.to_string()),
    );
    manifest_obj.insert("purpose".into(), Value::String(PACKAGE_PURPOSE.to_string()));
    manifest_obj.insert("backupId".into(), Value::String(backup_id.to_string()));
    manifest_obj.insert("encrypted".into(), Value::Bool(encrypted));
    manifest_obj.insert(
        "source".into(),
        json!({
            "createdAt": created_iso,
            "version": app_version,
            "revision": build_revision(),
            "platform": current_platform_label(),
        }),
    );
    manifest_obj.insert(
        "scope".into(),
        json!({
            "mode": context.mode,
            "projectIds": context.project_ids,
            "includeHistory": context.include_history,
            "includeDrafts": context.include_drafts,
        }),
    );
    manifest_obj.insert("contributors".into(), Value::Array(contributor_summaries));
    manifest_obj.insert(
        "coverage".into(),
        session
            .get("coverage")
            .cloned()
            .unwrap_or_else(|| json!({})),
    );
    manifest_obj.insert(
        "exclusions".into(),
        Value::Array(
            context
                .exclusions()
                .into_iter()
                .map(Value::String)
                .collect(),
        ),
    );
    if let Some(fe_meta) = frontend_meta {
        manifest_obj.insert("frontend".into(), fe_meta);
    }
    manifest_obj.insert("files".into(), Value::Array(manifest_files.clone()));

    let manifest_bytes = dumps_compact(&Value::Object(manifest_obj)).into_bytes();
    let mut manifest_hasher = Sha256::new();
    manifest_hasher.update(&manifest_bytes);
    let manifest_digest = hex_digest(&manifest_hasher.finalize());
    fs::write(staging.join("manifest.json"), &manifest_bytes)
        .map_err(|_| route_support::unhandled_error())?;

    let mut checksum_lines = Vec::with_capacity(manifest_files.len() + 1);
    for entry in &manifest_files {
        let sha = entry.get("sha256").and_then(Value::as_str).unwrap_or("");
        let path = entry.get("path").and_then(Value::as_str).unwrap_or("");
        checksum_lines.push(format!("{sha}  {path}"));
    }
    checksum_lines.push(format!("{manifest_digest}  manifest.json"));
    let checksums_content = format!("{}\n", checksum_lines.join("\n"));
    fs::write(
        staging.join("checksums.sha256"),
        checksums_content.as_bytes(),
    )
    .map_err(|_| route_support::unhandled_error())?;

    let date_compact: String = created_iso
        .get(..10)
        .unwrap_or("2026-01-01")
        .chars()
        .filter(|ch| ch.is_ascii_digit())
        .collect();
    let suffix_chars: Vec<char> = backup_id.chars().collect();
    let suffix8: String = if suffix_chars.len() > 8 {
        suffix_chars[suffix_chars.len() - 8..].iter().collect()
    } else {
        backup_id.to_string()
    };
    let extension = if encrypted {
        ".dsibackup.age"
    } else {
        ".dsibackup"
    };
    let filename = format!("deepseek-infra-backup-{date_compact}-{suffix8}{extension}");
    let target = backup_dir.join(&filename);
    let mut target_guard = FileCleanupGuard::new(target.clone());

    let secret_str: Option<&str> = match secret {
        Some(bytes) => Some(bytes.as_str()?),
        None => None,
    };
    encrypt_tree_zip_native(&staging, &target, protection_mode, secret_str, recipients)?;

    if encrypted {
        let raw_secret = secret_str.ok_or_else(|| AppError {
            message: "Backup secret is required or expired".to_string(),
            code: codes::INVALID_REQUEST,
            status: 409,
        })?;
        if protection_mode == "passphrase" {
            decrypt_passphrase_file_native(&target, &verify_decrypted, raw_secret)?;
        } else {
            decrypt_file_native(&target, &verify_decrypted, raw_secret)?;
        }
        safe_extract_and_verify(&verify_decrypted, &verify_dir)?;
    } else {
        safe_extract_and_verify(&target, &verify_dir)?;
    }

    target_guard.disarm();
    let final_size = fs::metadata(&target)
        .map(|m| m.len())
        .map_err(|_| route_support::unhandled_error())?;
    Ok(BuildArchiveOutcome {
        filename,
        path: target,
        size: final_size,
        manifest_digest,
    })
}

fn safe_archive_name(raw: &str) -> String {
    let base = raw
        .rsplit(['/', '\\'])
        .next()
        .filter(|s| !s.is_empty())
        .unwrap_or("workspace.dsibackup");
    let lower = base.to_ascii_lowercase();
    let candidate = if lower.ends_with(".dsibackup.age") || lower.ends_with(".dsibackup") {
        base.to_string()
    } else {
        format!("{base}.dsibackup")
    };
    candidate.chars().take(120).collect()
}

fn is_age_archive(path: &Path) -> bool {
    let Ok(mut file) = File::open(path) else {
        return false;
    };
    let mut prefix = [0u8; 22];
    let Ok(read_len) = file.read(&mut prefix) else {
        return false;
    };
    prefix[..read_len].starts_with(b"age-encryption.org/v1")
}

pub(crate) fn tree_digest(root: &Path) -> Result<String, AppError> {
    fn collect(base: &Path, dir: &Path, out: &mut Vec<(String, PathBuf)>) -> Result<(), AppError> {
        let entries = fs::read_dir(dir).map_err(|_| route_support::unhandled_error())?;
        for entry in entries {
            let entry = entry.map_err(|_| route_support::unhandled_error())?;
            let path = entry.path();
            let meta = fs::symlink_metadata(&path).map_err(|_| route_support::unhandled_error())?;
            if meta.file_type().is_symlink() {
                continue;
            }
            if meta.is_dir() {
                collect(base, &path, out)?;
            } else if meta.is_file() {
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
    let mut files = Vec::new();
    collect(root, root, &mut files)?;
    files.sort_by(|left, right| left.0.cmp(&right.0));

    let mut outer = Sha256::new();
    let mut buf = [0u8; 64 * 1024];
    for (rel, path) in files {
        outer.update(rel.as_bytes());
        let mut inner = Sha256::new();
        let mut reader =
            BufReader::new(File::open(&path).map_err(|_| route_support::unhandled_error())?);
        loop {
            let n = reader
                .read(&mut buf)
                .map_err(|_| route_support::unhandled_error())?;
            if n == 0 {
                break;
            }
            inner.update(&buf[..n]);
        }
        outer.update(inner.finalize());
    }
    Ok(hex_digest(&outer.finalize()))
}

fn version_compatible(source: &str, target: &str) -> bool {
    fn parse(raw: &str) -> Option<(u64, u64, u64)> {
        let head = raw.split('-').next()?;
        let parts: Vec<&str> = head.split('.').collect();
        if parts.len() != 3 {
            return None;
        }
        Some((
            parts[0].parse().ok()?,
            parts[1].parse().ok()?,
            parts[2].parse().ok()?,
        ))
    }
    match (parse(source), parse(target)) {
        (Some(src), Some(dst)) => src.0 == dst.0 && (src.1, src.2) <= (dst.1, dst.2),
        _ => false,
    }
}

pub(crate) fn known_contributor(id: &str) -> Option<&'static ContributorSpec> {
    LOCAL_CONTRIBUTORS.iter().find(|spec| spec.id == id)
}

pub(crate) fn validate_staged_contributor(
    contributor_id: &str,
    source_dir: &Path,
) -> Result<usize, AppError> {
    if contributor_id == "stateless-mcp" {
        let state_file = source_dir.join("state.jsonl");
        if !state_file.is_file() {
            return Err(AppError::invalid_payload(
                "stateless-mcp: snapshot is missing",
            ));
        }
        let raw = fs::read_to_string(&state_file)
            .map_err(|_| AppError::invalid_payload("stateless-mcp: snapshot is invalid"))?;
        let mut complete = false;
        for line in raw.lines() {
            let parsed: Value = serde_json::from_str(line)
                .map_err(|_| AppError::invalid_payload("stateless-mcp: snapshot is invalid"))?;
            if parsed.get("schemaVersion").and_then(Value::as_i64) != Some(1) {
                return Err(AppError::invalid_payload(
                    "stateless-mcp: unsupported snapshot schema",
                ));
            }
            if parsed.get("type").and_then(Value::as_str) == Some("complete") {
                complete = true;
            }
            let lowered = line.to_ascii_lowercase();
            for forbidden in ["redis://", "bearer ", "mcp_auth_token", "fencingtoken"] {
                if lowered.contains(forbidden) {
                    return Err(AppError::invalid_payload(
                        "stateless-mcp: snapshot contains forbidden deployment state",
                    ));
                }
            }
        }
        if !complete {
            return Err(AppError::invalid_payload(
                "stateless-mcp: snapshot is incomplete",
            ));
        }
        return Ok(1);
    }

    if !source_dir.is_dir() {
        return Ok(0);
    }
    fn walk_validate(base: &Path, dir: &Path, count: &mut usize) -> Result<(), AppError> {
        let entries = fs::read_dir(dir).map_err(|_| route_support::unhandled_error())?;
        for entry in entries {
            let entry = entry.map_err(|_| route_support::unhandled_error())?;
            let path = entry.path();
            let meta = fs::symlink_metadata(&path).map_err(|_| route_support::unhandled_error())?;
            if meta.file_type().is_symlink() {
                return Err(AppError::invalid_payload("Backup contains a symlink"));
            }
            if meta.is_dir() {
                walk_validate(base, &path, count)?;
            } else if meta.is_file() {
                let rel = path
                    .strip_prefix(base)
                    .map_err(|_| route_support::unhandled_error())?;
                if excluded_path(rel) {
                    return Err(AppError::invalid_payload(format!(
                        "Backup contributor contains excluded secret or ephemeral file: {}",
                        rel.to_string_lossy()
                    )));
                }
                *count += 1;
            }
        }
        Ok(())
    }
    let mut count = 0usize;
    walk_validate(source_dir, source_dir, &mut count)?;
    Ok(count)
}

fn count_relative_conflicts(source_dir: &Path, dest_dir: &Path) -> usize {
    if !source_dir.is_dir() || !dest_dir.is_dir() {
        return 0;
    }
    fn walk(src_base: &Path, dir: &Path, dst_base: &Path, conflicts: &mut usize) {
        let Ok(entries) = fs::read_dir(dir) else {
            return;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if path.is_dir() {
                walk(src_base, &path, dst_base, conflicts);
            } else if path.is_file() {
                if let Ok(rel) = path.strip_prefix(src_base) {
                    if dst_base.join(rel).is_file() {
                        *conflicts += 1;
                    }
                }
            }
        }
    }
    let mut conflicts = 0usize;
    walk(source_dir, source_dir, dest_dir, &mut conflicts);
    conflicts
}

struct InspectMeta<'a> {
    restore_id: &'a str,
    archive_sha256: String,
    encrypted: bool,
    ciphertext_sha256: Option<String>,
    protection: &'a str,
}

fn inspect_verified_tree(
    root: &Path,
    manifest: Value,
    extracted: &Path,
    restore_dir: &Path,
    meta: InspectMeta<'_>,
) -> Result<Value, AppError> {
    let source_version = manifest
        .get("source")
        .and_then(|s| s.get("version"))
        .and_then(Value::as_str)
        .unwrap_or("");
    let target_version = version_info().version;
    let mut compatible = version_compatible(source_version, target_version);

    let contributors = manifest
        .get("contributors")
        .and_then(Value::as_array)
        .ok_or_else(|| AppError::invalid_payload("Backup contributor inventory is invalid"))?;
    let scope_project_ids: Vec<String> = manifest
        .get("scope")
        .and_then(|s| s.get("projectIds"))
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .filter_map(|v| v.as_str().map(str::to_string))
                .collect()
        })
        .unwrap_or_default();

    let mut operations: Vec<Value> = Vec::new();
    let mut conflicts: Vec<Value> = Vec::new();
    let mut migrations: Vec<Value> = Vec::new();
    let mut requires_external_mcp = false;

    for entry in contributors {
        let Some(id) = entry.get("id").and_then(Value::as_str) else {
            return Err(AppError::invalid_payload(
                "Backup contributor inventory is invalid",
            ));
        };
        let schema_ver = entry
            .get("schemaVersion")
            .and_then(Value::as_i64)
            .unwrap_or(-1);
        let source_dir = extracted.join("payload").join(id);
        if id == "stateless-mcp" {
            requires_external_mcp = true;
            if schema_ver != 1 {
                compatible = false;
                migrations.push(json!({
                    "contributorId": id,
                    "from": schema_ver,
                    "to": 1,
                }));
            }
            validate_staged_contributor(id, &source_dir)?;
            let configured_url = std::env::var("STATELESS_MCP_BACKUP_URL")
                .unwrap_or_default()
                .trim()
                .to_string();
            let available = if configured_url.is_empty() {
                false
            } else {
                probe_external_available(&configured_url)
            };
            if !available {
                compatible = false;
            }
            operations.push(json!({
                "contributorId": "stateless-mcp",
                "source": source_dir.to_string_lossy().into_owned(),
                "destination": "stateless-mcp://durable-task-state",
                "files": 1,
                "conflicts": 0,
                "policy": "merge",
                "external": true,
                "available": available,
            }));
            continue;
        }

        let Some(spec) = known_contributor(id) else {
            return Err(AppError::invalid_payload(format!(
                "Unsupported contributor: {id}"
            )));
        };
        if schema_ver != spec.schema_version {
            compatible = false;
            migrations.push(json!({
                "contributorId": id,
                "from": schema_ver,
                "to": spec.schema_version,
            }));
        }
        let file_count = validate_staged_contributor(id, &source_dir)?;
        let dest_dir = root.join(spec.dir_name);
        let conflict_count = count_relative_conflicts(&source_dir, &dest_dir);
        let mut op = Map::new();
        op.insert("contributorId".into(), Value::String(id.to_string()));
        op.insert(
            "source".into(),
            Value::String(source_dir.to_string_lossy().into_owned()),
        );
        op.insert(
            "destination".into(),
            Value::String(dest_dir.to_string_lossy().into_owned()),
        );
        op.insert("files".into(), json!(file_count));
        op.insert("conflicts".into(), json!(conflict_count));
        op.insert("policy".into(), Value::String("merge".to_string()));
        if id == "projects" && !scope_project_ids.is_empty() {
            op.insert("projectScope".into(), json!(scope_project_ids));
        }
        operations.push(Value::Object(op));
        if conflict_count > 0 {
            conflicts.push(json!({
                "contributorId": id,
                "count": conflict_count,
                "strategy": "deterministic-remap",
            }));
        }
    }

    let estimated_bytes: u64 = manifest
        .get("files")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(|item| item.get("size").and_then(Value::as_u64))
                .sum()
        })
        .unwrap_or(0);
    let requires_frontend = manifest.get("frontend").is_some_and(|v| !v.is_null())
        || extracted.join("frontend").join("state.json").is_file()
        || extracted
            .join("frontend")
            .join("sealed-state.age")
            .is_file();
    let manifest_digest = sha256_file(&extracted.join("manifest.json"))
        .map_err(|_| route_support::unhandled_error())?;
    let extracted_tree_digest = tree_digest(extracted)?;
    let warnings = if meta.encrypted {
        Vec::new()
    } else {
        vec![Value::String(
            "Backup is integrity-verified but not encrypted.".to_string(),
        )]
    };

    let mut plan = Map::new();
    plan.insert(
        "restoreId".into(),
        Value::String(meta.restore_id.to_string()),
    );
    plan.insert(
        "sourceVersion".into(),
        Value::String(source_version.to_string()),
    );
    plan.insert(
        "targetVersion".into(),
        Value::String(target_version.to_string()),
    );
    plan.insert("compatible".into(), Value::Bool(compatible));
    plan.insert(
        "purpose".into(),
        manifest
            .get("purpose")
            .cloned()
            .unwrap_or_else(|| Value::String(PACKAGE_PURPOSE.to_string())),
    );
    plan.insert("operations".into(), Value::Array(operations));
    plan.insert("conflicts".into(), Value::Array(conflicts));
    plan.insert("migrations".into(), Value::Array(migrations));
    plan.insert("warnings".into(), Value::Array(warnings));
    plan.insert("estimatedWriteBytes".into(), json!(estimated_bytes));
    plan.insert(
        "requiresFrontendApply".into(),
        Value::Bool(requires_frontend),
    );
    plan.insert(
        "requiresExternalMcp".into(),
        Value::Bool(requires_external_mcp),
    );
    plan.insert("archiveSha256".into(), Value::String(meta.archive_sha256));
    plan.insert(
        "ciphertextSha256".into(),
        match meta.ciphertext_sha256 {
            Some(sha) => Value::String(sha),
            None => Value::Null,
        },
    );
    plan.insert("encrypted".into(), Value::Bool(meta.encrypted));
    plan.insert(
        "protection".into(),
        Value::String(meta.protection.to_string()),
    );
    plan.insert(
        "coverage".into(),
        manifest
            .get("coverage")
            .cloned()
            .unwrap_or_else(|| json!({})),
    );
    plan.insert("manifest".into(), manifest);
    plan.insert("manifestDigest".into(), Value::String(manifest_digest));
    plan.insert(
        "extractedTreeDigest".into(),
        Value::String(extracted_tree_digest),
    );
    plan.insert("phase".into(), Value::String("inspected".to_string()));
    if meta.encrypted {
        plan.insert("secretArmedAt".into(), json!(now_unix_secs_f64()));
    }

    write_atomic_json(&restore_dir.join("plan.json"), &Value::Object(plan.clone()))?;
    let mut out = plan;
    out.insert("ok".into(), Value::Bool(true));
    Ok(Value::Object(out))
}

fn probe_external_available(base_url: &str) -> bool {
    let url = format!(
        "{}/internal/backups/capabilities",
        base_url.trim_end_matches('/')
    );
    let Ok(client) = reqwest::blocking::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(2))
        .build()
    else {
        return false;
    };
    let mut req = client.get(url);
    let token = std::env::var("STATELESS_MCP_BACKUP_TOKEN")
        .unwrap_or_default()
        .trim()
        .to_string();
    if !token.is_empty() {
        req = req.bearer_auth(token);
    }
    let Ok(resp) = req.send() else {
        return false;
    };
    if !resp.status().is_success() {
        return false;
    }
    let Ok(raw) = resp.text() else {
        return false;
    };
    let Ok(body) = serde_json::from_str::<Value>(&raw) else {
        return false;
    };
    body.get("contributorId").and_then(Value::as_str) == Some("stateless-mcp")
        && body.get("schemaVersion").and_then(Value::as_i64) == Some(1)
}

fn inspect_staged_archive(
    root: &Path,
    staged_path: &Path,
    filename: &str,
    expected_sha256: &str,
) -> Result<Value, AppError> {
    let restore_hex = SystemEntropy.new_id()?;
    let restore_id = format!("restore_{restore_hex}");
    validate_restore_id(&restore_id)?;
    let restore_dir = root.join(".restore-staging").join(&restore_id);
    fs::create_dir_all(&restore_dir).map_err(|_| route_support::unhandled_error())?;
    let mut dir_guard = DirCleanupGuard::new(restore_dir.clone());

    let safe_name = safe_archive_name(filename);
    let archive_path = restore_dir.join(&safe_name);
    fs::copy(staged_path, &archive_path).map_err(|_| route_support::unhandled_error())?;
    let actual_sha = sha256_file(&archive_path).map_err(|_| route_support::unhandled_error())?;
    if actual_sha != expected_sha256 {
        return Err(AppError::invalid_payload("Backup upload digest mismatch"));
    }

    if is_age_archive(&archive_path) {
        let is_passphrase = inspect_age_file_protection(&archive_path)
            .map_err(|_| AppError::invalid_payload("Backup archive is unreadable"))?;
        let protection = if is_passphrase {
            "passphrase"
        } else {
            "age-recipient"
        };
        let upload_doc = json!({
            "restoreId": restore_id,
            "phase": "locked",
            "filename": safe_name,
            "protection": protection,
            "ciphertextSha256": actual_sha,
            "createdAt": now_utc_iso(),
        });
        write_atomic_json(&restore_dir.join("upload.json"), &upload_doc)?;
        dir_guard.disarm();
        let mut out = upload_doc.as_object().cloned().unwrap_or_default();
        out.insert("ok".into(), Value::Bool(true));
        return Ok(Value::Object(out));
    }

    let extracted = restore_dir.join("extracted");
    let manifest = safe_extract_and_verify(&archive_path, &extracted)?;
    fsync_dir(&restore_dir);
    let result = inspect_verified_tree(
        root,
        manifest,
        &extracted,
        &restore_dir,
        InspectMeta {
            restore_id: &restore_id,
            archive_sha256: actual_sha,
            encrypted: false,
            ciphertext_sha256: None,
            protection: "none",
        },
    )?;
    dir_guard.disarm();
    Ok(result)
}

fn find_ciphertext_file(restore_dir: &Path, upload_filename: Option<&str>) -> Option<PathBuf> {
    if let Some(name) = upload_filename {
        if !name.is_empty() && !name.contains(['/', '\\']) && name != ".." && name != "." {
            let candidate = restore_dir.join(name);
            if candidate.is_file() {
                return Some(candidate);
            }
        }
    }
    let entries = fs::read_dir(restore_dir).ok()?;
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_file() {
            if let Some(name) = path.file_name().and_then(|n| n.to_str()) {
                if name.to_ascii_lowercase().ends_with(".dsibackup.age") || is_age_archive(&path) {
                    return Some(path);
                }
            }
        }
    }
    None
}

fn unlock_restore(root: &Path, restore_id: &str) -> Result<Value, AppError> {
    validate_restore_id(restore_id)?;
    let dir = restore_root(root, restore_id)?;
    let plan_path = dir.join("plan.json");
    let upload_path = dir.join("upload.json");

    if plan_path.is_file() {
        let mut plan = read_json_object(&plan_path)?;
        if !plan.get("encrypted").is_some_and(python_truthy) {
            plan.insert("ok".into(), Value::Bool(true));
            return Ok(Value::Object(plan));
        }
        return reattach_restore_secret(root, restore_id, &dir, &plan_path, plan);
    }
    if !upload_path.is_file() {
        return Err(AppError::not_found("Restore plan not found"));
    }

    let upload = read_json_object(&upload_path)?;
    let upload_filename = upload.get("filename").and_then(Value::as_str);
    let ciphertext = find_ciphertext_file(&dir, upload_filename)
        .ok_or_else(|| AppError::invalid_payload("Encrypted backup upload is missing"))?;
    let expected_cipher = upload
        .get("ciphertextSha256")
        .and_then(Value::as_str)
        .unwrap_or("");
    let actual_cipher =
        sha256_file(&ciphertext).map_err(|_| AppError::invalid_payload("Unable to read backup"))?;
    if expected_cipher.is_empty() || actual_cipher != expected_cipher {
        return Err(AppError::invalid_payload("Encrypted backup upload changed"));
    }

    let protection = upload
        .get("protection")
        .and_then(Value::as_str)
        .unwrap_or("age-recipient")
        .to_string();
    let kind = if protection == "passphrase" {
        "passphrase"
    } else {
        "age-identity"
    };
    let (_slot_kind, secret) = consume_secret(restore_id, Some(kind))?;

    let unlocked = dir.join("unlocked.dsibackup");
    let extracted = dir.join("extracted");
    let _unlocked_guard = FileCleanupGuard::new(unlocked.clone());

    let unlock_attempt = (|| -> Result<Value, AppError> {
        let secret_str = secret.as_str()?;
        if kind == "passphrase" {
            decrypt_passphrase_file_native(&ciphertext, &unlocked, secret_str)?;
        } else {
            decrypt_file_native(&ciphertext, &unlocked, secret_str)?;
        }
        let manifest = safe_extract_and_verify(&unlocked, &extracted)?;
        let archive_sha = sha256_file(&unlocked).map_err(|_| route_support::unhandled_error())?;
        let mut plan_value = inspect_verified_tree(
            root,
            manifest,
            &extracted,
            &dir,
            InspectMeta {
                restore_id,
                archive_sha256: archive_sha,
                encrypted: true,
                ciphertext_sha256: Some(actual_cipher),
                protection: &protection,
            },
        )?;
        if kind == "age-identity" {
            if let Some(sealed) = unlock_sealed_frontend(&dir, secret_str)? {
                if let Some(plan_obj) = plan_value.as_object_mut() {
                    plan_obj.insert("sealedFrontend".into(), sealed);
                }
                let mut stored = read_json_object(&plan_path)?;
                if let Some(sealed_val) = plan_value.get("sealedFrontend") {
                    stored.insert("sealedFrontend".into(), sealed_val.clone());
                    write_atomic_json(&plan_path, &Value::Object(stored))?;
                }
            }
        }
        put_secret_bytes(restore_id, kind, secret.as_slice())?;
        Ok(plan_value)
    })();

    if unlock_attempt.is_err() {
        let _ = fs::remove_dir_all(&extracted);
        record_unlock_failure(restore_id);
        return Err(AppError::invalid_payload("Unable to unlock backup"));
    }
    unlock_attempt
}

fn reattach_restore_secret(
    _root: &Path,
    restore_id: &str,
    dir: &Path,
    plan_path: &Path,
    mut plan: Map<String, Value>,
) -> Result<Value, AppError> {
    let upload_filename = dir
        .join("upload.json")
        .is_file()
        .then(|| read_json_object(&dir.join("upload.json")).ok())
        .flatten()
        .and_then(|obj| {
            obj.get("filename")
                .and_then(Value::as_str)
                .map(str::to_string)
        });
    let ciphertext = find_ciphertext_file(dir, upload_filename.as_deref())
        .ok_or_else(|| AppError::invalid_payload("Encrypted backup upload changed"))?;
    let expected_cipher = plan
        .get("ciphertextSha256")
        .and_then(Value::as_str)
        .unwrap_or("");
    let actual_cipher = sha256_file(&ciphertext)
        .map_err(|_| AppError::invalid_payload("Encrypted backup upload changed"))?;
    if expected_cipher.is_empty() || actual_cipher != expected_cipher {
        return Err(AppError::invalid_payload("Encrypted backup upload changed"));
    }

    let protection = plan
        .get("protection")
        .and_then(Value::as_str)
        .unwrap_or("age-recipient");
    let kind = if protection == "passphrase" {
        "passphrase"
    } else {
        "age-identity"
    };
    let (_slot_kind, secret) = consume_secret(restore_id, Some(kind))?;

    let reattach_tmp = dir.join("reattach.dsibackup");
    let _tmp_guard = FileCleanupGuard::new(reattach_tmp.clone());
    let verified = (|| -> Result<(), AppError> {
        let secret_str = secret.as_str()?;
        if kind == "passphrase" {
            decrypt_passphrase_file_native(&ciphertext, &reattach_tmp, secret_str)?;
        } else {
            decrypt_file_native(&ciphertext, &reattach_tmp, secret_str)?;
        }
        let actual_plain = sha256_file(&reattach_tmp)
            .map_err(|_| AppError::invalid_payload("Unable to unlock backup"))?;
        let expected_plain = plan
            .get("archiveSha256")
            .and_then(Value::as_str)
            .unwrap_or("");
        if expected_plain.is_empty() || actual_plain != expected_plain {
            return Err(AppError::invalid_payload("Unable to unlock backup"));
        }
        Ok(())
    })();
    if verified.is_err() {
        record_unlock_failure(restore_id);
        return Err(AppError::invalid_payload("Unable to unlock backup"));
    }

    plan.insert("secretArmedAt".into(), json!(now_unix_secs_f64()));
    write_atomic_json(plan_path, &Value::Object(plan.clone()))?;
    put_secret_bytes(restore_id, kind, secret.as_slice())?;
    plan.insert("ok".into(), Value::Bool(true));
    Ok(Value::Object(plan))
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[test]
    fn finalize_inspect_and_unlock_round_trip_plain_and_passphrase() {
        let dir = tempdir().unwrap();
        let root = dir.path();
        fs::create_dir_all(root.join(".projects/p1")).unwrap();
        fs::write(
            root.join(".projects/p1/project.json"),
            br#"{"id":"p1","name":"P1"}"#,
        )
        .unwrap();

        let create_payload: Map<String, Value> = serde_json::from_value(json!({
            "mode": "full",
            "requiresFrontendState": false,
            "coveragePolicy": "best-effort",
            "protection": {"mode": "passphrase"},
        }))
        .unwrap();
        let created =
            crate::backup_restore_write_routes::create_session(root, &create_payload).unwrap();
        let backup_id = created["backupId"].as_str().unwrap().to_string();
        crate::backup_secret_slots::put_secret(&backup_id, "passphrase", "correct-pw").unwrap();

        let finalized = finalize_session(root, &backup_id).unwrap();
        assert_eq!(finalized["phase"], "ready");
        assert!(
            finalized["filename"]
                .as_str()
                .unwrap()
                .ends_with(".dsibackup.age")
        );
        let archive_path = root
            .join(".backups")
            .join(finalized["filename"].as_str().unwrap());
        assert!(archive_path.is_file());
        // Consumed secret must no longer remain armed on backup session.
        assert!(!crate::backup_secret_slots::has_secret(&backup_id));

        let sha = sha256_file(&archive_path).unwrap();
        let locked = inspect_staged_archive(
            root,
            &archive_path,
            finalized["filename"].as_str().unwrap(),
            &sha,
        )
        .unwrap();
        assert_eq!(locked["phase"], "locked");
        assert_eq!(locked["protection"], "passphrase");
        let restore_id = locked["restoreId"].as_str().unwrap().to_string();

        // Missing secret fails with 409.
        let err = unlock_restore(root, &restore_id).unwrap_err();
        assert_eq!(err.status, 409);

        // Wrong passphrase fails with 400 and leaves extracted dir absent.
        crate::backup_secret_slots::put_secret(&restore_id, "passphrase", "wrong").unwrap();
        let bad = unlock_restore(root, &restore_id).unwrap_err();
        assert_eq!(bad.status, 400);
        assert!(
            !root
                .join(".restore-staging")
                .join(&restore_id)
                .join("extracted")
                .exists()
        );

        // Correct passphrase unlocks, removes plaintext unlocked.dsibackup, and re-arms secret slot.
        crate::backup_secret_slots::put_secret(&restore_id, "passphrase", "correct-pw").unwrap();
        let plan = unlock_restore(root, &restore_id).unwrap();
        assert_eq!(plan["phase"], "inspected");
        assert_eq!(plan["encrypted"], Value::Bool(true));
        assert!(crate::backup_secret_slots::has_secret(&restore_id));
        assert!(
            !root
                .join(".restore-staging")
                .join(&restore_id)
                .join("unlocked.dsibackup")
                .exists()
        );

        // Reattaching secret when already unlocked preserves manifest and validates ciphertext.
        crate::backup_secret_slots::clear_secret(&restore_id);
        crate::backup_secret_slots::put_secret(&restore_id, "passphrase", "correct-pw").unwrap();
        let reattached = unlock_restore(root, &restore_id).unwrap();
        assert_eq!(reattached["phase"], "inspected");
        assert!(crate::backup_secret_slots::has_secret(&restore_id));
    }

    #[test]
    fn check_version_compatibility_rules() {
        assert!(version_compatible("4.4.0", "4.8.0"));
        assert!(version_compatible("4.8.0", "4.8.0"));
        assert!(!version_compatible("5.0.0", "4.8.0"));
        assert!(!version_compatible("4.4", "4.8.0"));
        assert!(!version_compatible("bad", "4.8.0"));
    }
}
