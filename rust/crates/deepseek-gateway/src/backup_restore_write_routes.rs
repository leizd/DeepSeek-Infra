//! Native S5 backup session and restore staging mutations under `restore_staging_store`:
//! - `POST /api/workspace/backups` (`create_session`)
//! - `PUT /api/workspace/backups/{backup_id}/secret` (`put_session_secret`)
//! - `PUT /api/workspace/backups/{backup_id}/frontend-state` (`put_frontend_state`)
//! - `DELETE /api/workspace/backups/{backup_id}` (`delete_backup`)
//! - `PUT /api/workspace/restores/{restore_id}/secret` (`put_session_secret`)
//! - `DELETE /api/workspace/restores/{restore_id}` (`delete_restore`)
//! - `POST /api/workspace/restores/cleanup` (`cleanup_restores`)
//!
//! Write authority follows `DECLARED_NATIVE_DATA_DOMAINS`: mutations execute
//! only when `DEEPSEEK_RUNTIME_MODE=python_disabled` (`may_write_native_store`
//! for `restore_staging_store`) and fail closed with `409 NATIVE_RESTORE_STAGING_STORE_WRITE_NOT_OWNED`
//! without touching disk otherwise.

use std::collections::BTreeSet;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use axum::body::Bytes;
use axum::extract::Path as AxumPath;
use axum::http::{HeaderMap, Method};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use axum::{Json, Router};
use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::backup_mirror::fsync_dir;
use deepseek_policy::core_utils::python_truthy;
use deepseek_policy::entropy::{Entropy, SystemEntropy};
use deepseek_policy::mutation_gate;
use deepseek_policy::python_json::{dumps_compact, value_str};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use crate::backup_capacity_routes::now_utc_iso;
use crate::backup_catalog_routes::parse_python_iso;
use crate::backup_restore_read_routes::{
    list_restores, load_session, now_unix_secs_f64, read_json_object, restore_root,
    validate_backup_id, validate_restore_id,
};
use crate::backup_secret_slots::{clear_secret, put_secret};
use crate::route_support;

pub(crate) const RESTORE_STAGING_DOMAIN: &str = "restore_staging_store";
pub(crate) const RESTORE_STAGING_WRITE_NOT_OWNED: &str =
    "NATIVE_RESTORE_STAGING_STORE_WRITE_NOT_OWNED";

const MAX_CREATE_BYTES: i64 = 64_000;
const MAX_SECRET_BODY_BYTES: i64 = 128_000;
const MAX_FRONTEND_BODY_BYTES: i64 = 4_000_000;
const FRONTEND_SCHEMA_VERSION: i64 = 1;
const MAX_JSON_DEPTH: usize = 64;

const FORBIDDEN_FRONTEND_KEYS: [&str; 6] = [
    "apiKey",
    "tavilyKey",
    "authorizationToken",
    "writerSessionId",
    "documentInstanceId",
    "lease",
];

const SECRET_NAMES: [&str; 9] = [
    ".auth-token",
    ".env",
    "api-key",
    "api_key",
    "auth-token",
    "bearer-token",
    "credentials",
    "credentials.json",
    "tavily-key",
];

const EPHEMERAL_SUFFIXES: [&str; 4] = [".lock", ".pid", ".tmp", ".part"];

struct ContributorSpec {
    id: &'static str,
    schema_version: i64,
    data_class: &'static str,
    dir_name: &'static str,
}

const LOCAL_CONTRIBUTORS: [ContributorSpec; 11] = [
    ContributorSpec {
        id: "projects",
        schema_version: 1,
        data_class: "durable",
        dir_name: ".projects",
    },
    ContributorSpec {
        id: "project-files",
        schema_version: 1,
        data_class: "durable",
        dir_name: ".file-cache",
    },
    ContributorSpec {
        id: "artifacts",
        schema_version: 1,
        data_class: "durable",
        dir_name: ".generated",
    },
    ContributorSpec {
        id: "memory",
        schema_version: 1,
        data_class: "durable",
        dir_name: ".memory",
    },
    ContributorSpec {
        id: "media",
        schema_version: 1,
        data_class: "durable",
        dir_name: ".media",
    },
    ContributorSpec {
        id: "custom-skills-and-packs",
        schema_version: 1,
        data_class: "durable",
        dir_name: ".skills",
    },
    ContributorSpec {
        id: "automations",
        schema_version: 1,
        data_class: "durable",
        dir_name: ".automation",
    },
    ContributorSpec {
        id: "reminders",
        schema_version: 1,
        data_class: "durable",
        dir_name: ".reminders",
    },
    ContributorSpec {
        id: "agent-checkpoints",
        schema_version: 1,
        data_class: "optional-history",
        dir_name: ".agent-runs",
    },
    ContributorSpec {
        id: "a2a-tasks",
        schema_version: 1,
        data_class: "optional-history",
        dir_name: ".a2a",
    },
    ContributorSpec {
        id: "traces-and-run-history",
        schema_version: 1,
        data_class: "optional-history",
        dir_name: ".traces",
    },
];

static TEMP_SEQ: AtomicU64 = AtomicU64::new(0);

pub fn router() -> Router {
    Router::new()
        .route("/api/workspace/backups", any(dispatch_create_backup))
        .route(
            "/api/workspace/backups/:backup_id/secret",
            any(dispatch_put_backup_secret),
        )
        .route(
            "/api/workspace/backups/:backup_id/frontend-state",
            any(dispatch_put_frontend_state),
        )
        .route(
            "/api/workspace/restores/:restore_id/secret",
            any(dispatch_put_restore_secret),
        )
        .route(
            "/api/workspace/restores/cleanup",
            any(dispatch_cleanup_restores),
        )
}

async fn dispatch_create_backup(method: Method, headers: HeaderMap, body: Bytes) -> Response {
    if method != Method::POST {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    if let Err(err) = require_write_authority() {
        return route_support::app_error_response(err);
    }
    let payload = match route_support::read_json_object(&headers, &body, MAX_CREATE_BYTES) {
        Ok(map) => map,
        Err(err) => return route_support::app_error_response(err),
    };
    let joined = tokio::task::spawn_blocking(move || {
        let root = crate::data_routes::workspace_root();
        create_session(&root, &payload)
    })
    .await;
    finish_json(joined)
}

async fn dispatch_put_backup_secret(
    method: Method,
    AxumPath(backup_id): AxumPath<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if method != Method::PUT {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    if let Err(err) = require_write_authority() {
        return route_support::app_error_response(err);
    }
    let payload = match route_support::read_json_object(&headers, &body, MAX_SECRET_BODY_BYTES) {
        Ok(map) => map,
        Err(err) => return route_support::app_error_response(err),
    };
    let joined = tokio::task::spawn_blocking(move || {
        let root = crate::data_routes::workspace_root();
        put_backup_secret(&root, &backup_id, &payload)
    })
    .await;
    finish_json(joined)
}

async fn dispatch_put_restore_secret(
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
    let payload = match route_support::read_json_object(&headers, &body, MAX_SECRET_BODY_BYTES) {
        Ok(map) => map,
        Err(err) => return route_support::app_error_response(err),
    };
    let joined = tokio::task::spawn_blocking(move || {
        let root = crate::data_routes::workspace_root();
        put_restore_secret(&root, &restore_id, &payload)
    })
    .await;
    finish_json(joined)
}

async fn dispatch_put_frontend_state(
    method: Method,
    AxumPath(backup_id): AxumPath<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if method != Method::PUT {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    if let Err(err) = require_write_authority() {
        return route_support::app_error_response(err);
    }
    let payload = match route_support::read_json_object(&headers, &body, MAX_FRONTEND_BODY_BYTES) {
        Ok(map) => map,
        Err(err) => return route_support::app_error_response(err),
    };
    let joined = tokio::task::spawn_blocking(move || {
        let root = crate::data_routes::workspace_root();
        put_frontend_state(&root, &backup_id, &payload)
    })
    .await;
    finish_json(joined)
}

pub(crate) async fn dispatch_delete_backup(backup_id: String) -> Response {
    if let Err(err) = require_write_authority() {
        return route_support::app_error_response(err);
    }
    let joined = tokio::task::spawn_blocking(move || {
        let root = crate::data_routes::workspace_root();
        delete_backup(&root, &backup_id)
    })
    .await;
    finish_json(joined)
}

pub(crate) async fn dispatch_delete_restore(restore_id: String) -> Response {
    if let Err(err) = require_write_authority() {
        return route_support::app_error_response(err);
    }
    let joined = tokio::task::spawn_blocking(move || {
        let root = crate::data_routes::workspace_root();
        delete_restore(&root, &restore_id)
    })
    .await;
    finish_json(joined)
}

async fn dispatch_cleanup_restores(method: Method) -> Response {
    if method != Method::POST {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    if let Err(err) = require_write_authority() {
        return route_support::app_error_response(err);
    }
    let joined = tokio::task::spawn_blocking(move || {
        let root = crate::data_routes::workspace_root();
        cleanup_restores(&root)
    })
    .await;
    finish_json(joined)
}

fn require_write_authority() -> Result<(), AppError> {
    route_support::require_native_store(RESTORE_STAGING_DOMAIN, RESTORE_STAGING_WRITE_NOT_OWNED)
}

fn finish_json(joined: Result<Result<Value, AppError>, tokio::task::JoinError>) -> Response {
    match joined {
        Ok(Ok(value)) => Json(value).into_response(),
        Ok(Err(error)) => route_support::app_error_response(error),
        Err(_) => route_support::app_error_response(route_support::unhandled_error()),
    }
}

fn sha256_compact_json(value: &Value) -> String {
    let encoded = dumps_compact(value);
    let digest = Sha256::digest(encoded.as_bytes());
    let mut out = String::with_capacity(64);
    for byte in digest {
        out.push_str(&format!("{byte:02x}"));
    }
    out
}

fn write_atomic_json(path: &Path, value: &Value) -> Result<(), AppError> {
    let parent = path.parent().ok_or_else(route_support::unhandled_error)?;
    fs::create_dir_all(parent).map_err(|_| route_support::unhandled_error())?;
    let seq = TEMP_SEQ.fetch_add(1, Ordering::Relaxed);
    let pid = std::process::id();
    let file_name = path
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("state.json");
    let temp = parent.join(format!("{file_name}.{pid}.{seq}.tmp"));
    let encoded = dumps_compact(value);
    let result = (|| -> std::io::Result<()> {
        {
            let mut file = OpenOptions::new()
                .create(true)
                .truncate(true)
                .write(true)
                .open(&temp)?;
            file.write_all(encoded.as_bytes())?;
            file.flush()?;
            file.sync_all()?;
        }
        fs::rename(&temp, path)?;
        let _ = fsync_dir(parent);
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temp);
    }
    result.map_err(|_| route_support::unhandled_error())
}

struct BackupContext {
    mode: String,
    project_ids: Vec<String>,
    include_history: bool,
    include_drafts: bool,
    include_rebuildable_indexes: bool,
    coverage_policy: String,
    include_external_state: bool,
}

impl BackupContext {
    fn to_json(&self) -> Value {
        json!({
            "mode": self.mode,
            "projectIds": self.project_ids,
            "includeHistory": self.include_history,
            "includeDrafts": self.include_drafts,
            "includeRebuildableIndexes": self.include_rebuildable_indexes,
            "coveragePolicy": self.coverage_policy,
            "includeExternalState": self.include_external_state,
        })
    }

    fn exclusions(&self) -> Vec<String> {
        let mut items = vec![
            "credentials and auth tokens".to_string(),
            "ephemeral locks and process state".to_string(),
            "rebuildable indexes".to_string(),
            "active task replay".to_string(),
        ];
        if !self.include_history {
            items.push("optional run history".to_string());
        }
        if !self.include_drafts {
            items.push("frontend drafts".to_string());
        }
        items
    }
}

fn parse_context(payload: &Map<String, Value>) -> Result<BackupContext, AppError> {
    let mode = payload
        .get("mode")
        .filter(|value| python_truthy(value))
        .map(value_str)
        .unwrap_or_else(|| "full".to_string());
    if mode != "full" && mode != "project" {
        return Err(AppError::invalid_payload(
            "Backup mode must be full or project",
        ));
    }
    let mut ids_set = BTreeSet::new();
    if let Some(Value::Array(raw_ids)) = payload.get("projectIds") {
        for value in raw_ids {
            if !value.is_null() {
                let text = value_str(value).trim().to_string();
                if !text.is_empty() {
                    ids_set.insert(text);
                }
            }
        }
    }
    if mode == "project" && ids_set.is_empty() {
        if let Some(single) = payload
            .get("projectId")
            .filter(|value| python_truthy(value))
            .map(|value| value_str(value).trim().to_string())
        {
            if !single.is_empty() {
                ids_set.insert(single);
            }
        }
    }
    if mode == "project" && ids_set.is_empty() {
        return Err(AppError::invalid_payload(
            "Project backup requires a project id",
        ));
    }
    let coverage_policy = payload
        .get("coveragePolicy")
        .filter(|value| python_truthy(value))
        .map(value_str)
        .unwrap_or_else(|| "strict".to_string());
    if coverage_policy != "strict" && coverage_policy != "best-effort" {
        return Err(AppError::invalid_payload(
            "Coverage policy must be strict or best-effort",
        ));
    }
    let bool_or = |key: &str, default: bool| -> bool {
        payload.get(key).map(python_truthy).unwrap_or(default)
    };
    Ok(BackupContext {
        mode,
        project_ids: ids_set.into_iter().collect(),
        include_history: bool_or("includeHistory", false),
        include_drafts: bool_or("includeDrafts", false),
        include_rebuildable_indexes: bool_or("includeRebuildableIndexes", false),
        coverage_policy,
        include_external_state: bool_or("includeExternalState", true),
    })
}

fn parse_protection(payload: &Map<String, Value>) -> Result<Value, AppError> {
    let raw = payload.get("protection");
    let obj = raw.and_then(Value::as_object);
    let mode = obj
        .and_then(|map| map.get("mode"))
        .filter(|value| python_truthy(value))
        .map(value_str)
        .unwrap_or_else(|| "none".to_string());
    if !matches!(mode.as_str(), "none" | "passphrase" | "age-recipient") {
        return Err(AppError::invalid_payload(
            "Unsupported backup protection mode",
        ));
    }
    let mut out = Map::new();
    out.insert("mode".into(), Value::String(mode.clone()));
    if mode == "age-recipient" {
        let mut recipients = Vec::new();
        if let Some(Value::Array(items)) = obj.and_then(|map| map.get("recipients")) {
            for item in items {
                if let Value::String(text) = item {
                    let trimmed = text.trim();
                    if !trimmed.is_empty() && !recipients.iter().any(|existing| existing == trimmed)
                    {
                        recipients.push(trimmed.to_string());
                    }
                }
            }
        }
        if recipients.is_empty()
            || recipients.len() > 16
            || recipients
                .iter()
                .any(|item| !item.starts_with("age1") || item.len() > 200)
        {
            return Err(AppError::invalid_payload(
                "One or more valid age recipients are required",
            ));
        }
        out.insert(
            "recipients".into(),
            Value::Array(recipients.into_iter().map(Value::String).collect()),
        );
    }
    Ok(Value::Object(out))
}

fn probe_stateless_mcp_capabilities(base_url: &str) -> Value {
    let url = format!(
        "{}/internal/backups/capabilities",
        base_url.trim_end_matches('/')
    );
    let fallback = || {
        json!({
            "id": "stateless-mcp",
            "available": false,
            "schemaVersion": 1,
            "reason": "service unavailable",
        })
    };
    let Ok(client) = reqwest::blocking::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(2))
        .build()
    else {
        return fallback();
    };
    let mut req = client
        .get(url)
        .header("Accept", "application/json, application/x-ndjson");
    let token = std::env::var("STATELESS_MCP_BACKUP_TOKEN")
        .unwrap_or_default()
        .trim()
        .to_string();
    if !token.is_empty() {
        req = req.bearer_auth(token);
    }
    let Ok(resp) = req.send() else {
        return fallback();
    };
    if !resp.status().is_success() {
        return fallback();
    }
    let Ok(body) = resp.json::<Value>() else {
        return fallback();
    };
    let available = body.get("contributorId").and_then(Value::as_str) == Some("stateless-mcp")
        && body.get("schemaVersion").and_then(Value::as_i64) == Some(1);
    json!({
        "id": "stateless-mcp",
        "available": available,
        "schemaVersion": 1,
    })
}

fn excluded_path(relative: &Path) -> bool {
    for component in relative.components() {
        if let Component::Normal(part) = component {
            if let Some(text) = part.to_str() {
                let lowered = text.to_lowercase();
                if SECRET_NAMES.iter().any(|secret| *secret == lowered) {
                    return true;
                }
            }
        }
    }
    relative
        .extension()
        .and_then(|ext| ext.to_str())
        .map(|ext| format!(".{}", ext.to_lowercase()))
        .is_some_and(|dot_ext| EPHEMERAL_SUFFIXES.iter().any(|suf| *suf == dot_ext))
}

fn collect_files_recursive(dir: &Path, base: &Path, out: &mut Vec<PathBuf>) {
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let Ok(meta) = fs::symlink_metadata(&path) else {
            continue;
        };
        if meta.file_type().is_symlink() {
            continue;
        }
        if meta.is_dir() {
            collect_files_recursive(&path, base, out);
        } else if meta.is_file() {
            let _ = base;
            out.push(path);
        }
    }
}

fn project_artifact_paths(root: &Path, project_ids: &[String]) -> BTreeSet<String> {
    let generated_dir = root.join(".generated");
    let canonical_gen = fs::canonicalize(&generated_dir).unwrap_or(generated_dir);
    let mut allowed = BTreeSet::new();

    fn visit(value: &Value, canonical_gen: &Path, allowed: &mut BTreeSet<String>) {
        match value {
            Value::Object(map) => {
                for item in map.values() {
                    visit(item, canonical_gen, allowed);
                }
            }
            Value::Array(items) => {
                for item in items {
                    visit(item, canonical_gen, allowed);
                }
            }
            Value::String(raw) => {
                let normalized = raw.replace('\\', "/");
                let rel = if let Some(rest) = normalized.strip_prefix(".generated/") {
                    rest.to_string()
                } else if let Ok(resolved) = fs::canonicalize(raw) {
                    if let Ok(stripped) = resolved.strip_prefix(canonical_gen) {
                        stripped.to_string_lossy().replace('\\', "/")
                    } else {
                        return;
                    }
                } else {
                    return;
                };
                if rel.is_empty() {
                    return;
                }
                let candidate = rel
                    .split('/')
                    .fold(canonical_gen.to_path_buf(), |acc, p| acc.join(p));
                if candidate.is_file() {
                    allowed.insert(rel);
                }
            }
            _ => {}
        }
    }

    for pid in project_ids {
        let project_root = root.join(".projects").join(pid);
        if !project_root.is_dir() {
            continue;
        }
        let mut files = Vec::new();
        collect_files_recursive(&project_root, &project_root, &mut files);
        for path in files {
            if path.extension().and_then(|ext| ext.to_str()) == Some("json") {
                if let Ok(text) = fs::read_to_string(&path) {
                    if let Ok(parsed) = serde_json::from_str::<Value>(&text) {
                        visit(&parsed, &canonical_gen, &mut allowed);
                    }
                }
            }
        }
    }
    allowed
}

fn estimate_contributor_bytes(root: &Path, spec: &ContributorSpec, context: &BackupContext) -> u64 {
    let contributor_root = root.join(spec.dir_name);
    if !contributor_root.exists() {
        return 0;
    }
    if context.mode == "project" && spec.id == "project-files" {
        return 0;
    }
    let allowed_artifacts = if context.mode == "project" && spec.id == "artifacts" {
        Some(project_artifact_paths(root, &context.project_ids))
    } else {
        None
    };
    let mut files = Vec::new();
    collect_files_recursive(&contributor_root, &contributor_root, &mut files);
    let mut total = 0u64;
    for path in files {
        let Ok(rel) = path.strip_prefix(&contributor_root) else {
            continue;
        };
        let mut first_part = None;
        if let Some(Component::Normal(first)) = rel.components().next() {
            first_part = first.to_str().map(str::to_string);
        }
        if context.mode == "project" && spec.id == "projects" {
            let matches = first_part
                .as_ref()
                .is_some_and(|part| context.project_ids.iter().any(|id| id == part));
            if !matches {
                continue;
            }
        }
        if spec.id == "artifacts"
            && first_part
                .as_ref()
                .is_some_and(|part| part.eq_ignore_ascii_case("workspace-exports"))
        {
            continue;
        }
        let posix_rel = rel.to_string_lossy().replace('\\', "/");
        if let Some(ref allowed) = allowed_artifacts {
            if !allowed.contains(&posix_rel) {
                continue;
            }
        }
        if excluded_path(rel) {
            continue;
        }
        if let Ok(meta) = fs::metadata(&path) {
            total = total.saturating_add(meta.len());
        }
    }
    total
}

fn build_contributor_plan(
    context: &BackupContext,
    created_at: &str,
) -> Result<(Value, Vec<&'static ContributorSpec>), AppError> {
    let mut entries = Vec::with_capacity(LOCAL_CONTRIBUTORS.len() + 1);
    let mut selected_specs = Vec::new();
    for spec in &LOCAL_CONTRIBUTORS {
        let selected = spec.data_class == "durable"
            || (spec.data_class == "optional-history" && context.include_history)
            || (spec.data_class == "rebuildable" && context.include_rebuildable_indexes);
        if selected {
            selected_specs.push(spec);
        }
        let cap_digest = sha256_compact_json(&json!({
            "id": spec.id,
            "schemaVersion": spec.schema_version,
            "dataClass": spec.data_class,
        }));
        entries.push(json!({
            "id": spec.id,
            "kind": "local",
            "schemaVersion": spec.schema_version,
            "required": spec.data_class == "durable",
            "capabilityDigest": cap_digest,
            "status": if selected { "selected" } else { "excluded" },
        }));
    }

    let configured_url = std::env::var("STATELESS_MCP_BACKUP_URL")
        .unwrap_or_default()
        .trim()
        .to_string();
    let mut status = "excluded";
    let mut reason = "not configured".to_string();
    let mut required = false;
    let mut capability = json!({
        "id": "stateless-mcp",
        "available": false,
        "schemaVersion": 1,
    });
    if !configured_url.is_empty() && context.include_external_state {
        required = context.mode == "full";
        reason = "service unavailable".to_string();
        if context.mode == "full" {
            capability = probe_stateless_mcp_capabilities(&configured_url);
            if capability.get("available").is_some_and(python_truthy) {
                status = "selected";
                reason.clear();
            } else {
                if context.coverage_policy == "strict" {
                    return Err(AppError {
                        message: "Strict backup coverage requires Stateless MCP durable state"
                            .to_string(),
                        code: codes::INVALID_REQUEST,
                        status: 409,
                    });
                }
                status = "unavailable";
                reason = capability
                    .get("reason")
                    .filter(|value| python_truthy(value))
                    .map(value_str)
                    .unwrap_or_else(|| "service unavailable".to_string());
            }
        } else {
            capability = probe_stateless_mcp_capabilities(&configured_url);
            status = "excluded";
            reason = "project-scoped backup".to_string();
        }
    } else if !configured_url.is_empty() {
        required = true;
        reason = "excluded by backup request".to_string();
    }

    let ext_digest = sha256_compact_json(&capability);
    let mut ext_entry = Map::new();
    ext_entry.insert("id".into(), Value::String("stateless-mcp".to_string()));
    ext_entry.insert("kind".into(), Value::String("external".to_string()));
    ext_entry.insert("schemaVersion".into(), Value::from(1));
    ext_entry.insert("required".into(), Value::Bool(required));
    ext_entry.insert("capabilityDigest".into(), Value::String(ext_digest));
    ext_entry.insert("status".into(), Value::String(status.to_string()));
    if !reason.is_empty() {
        ext_entry.insert("reason".into(), Value::String(reason));
    }
    entries.push(Value::Object(ext_entry));

    let plan_hex = SystemEntropy.new_id()?;
    let plan = json!({
        "planId": format!("plan_{plan_hex}"),
        "createdAt": created_at,
        "coveragePolicy": context.coverage_policy,
        "contributors": entries,
    });
    Ok((plan, selected_specs))
}

fn coverage_from_plan(plan: &Value) -> Value {
    let entries = plan
        .get("contributors")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let mut local: Vec<String> = Vec::new();
    let mut external = Vec::new();
    let mut unavailable = Vec::new();
    for entry in entries {
        let Some(obj) = entry.as_object() else {
            continue;
        };
        let kind = obj.get("kind").and_then(Value::as_str).unwrap_or("");
        let status = obj.get("status").and_then(Value::as_str).unwrap_or("");
        let id = obj.get("id").and_then(Value::as_str).unwrap_or("");
        if kind == "local" && status == "selected" {
            local.push(id.to_string());
        } else if kind == "external" && status == "selected" {
            external.push(json!({
                "id": id,
                "status": status,
                "schemaVersion": obj.get("schemaVersion").and_then(Value::as_i64).unwrap_or(1),
            }));
        }
        if obj.get("required").is_some_and(python_truthy) && status != "selected" {
            let reason = obj
                .get("reason")
                .filter(|value| python_truthy(value))
                .map(value_str)
                .unwrap_or_else(|| "unavailable".to_string());
            unavailable.push(json!({"id": id, "reason": reason}));
        }
    }
    local.sort();
    let complete = unavailable.is_empty();
    json!({
        "policy": plan.get("coveragePolicy").and_then(Value::as_str).unwrap_or("strict"),
        "planId": plan.get("planId").and_then(Value::as_str).unwrap_or(""),
        "localContributors": local,
        "externalContributors": external,
        "unavailableDurableSources": unavailable,
        "complete": complete,
    })
}

fn create_session(root: &Path, payload: &Map<String, Value>) -> Result<Value, AppError> {
    let context = parse_context(payload)?;
    let protection = parse_protection(payload)?;
    let created_at = now_utc_iso();
    let (plan, selected_specs) = build_contributor_plan(&context, &created_at)?;
    let backup_hex = SystemEntropy.new_id()?;
    let backup_id = format!("backup_{backup_hex}");
    validate_backup_id(&backup_id)?;
    let session_dir = root.join(".backups").join("sessions").join(&backup_id);
    if session_dir.exists() {
        return Err(route_support::unhandled_error());
    }
    fs::create_dir_all(&session_dir).map_err(|_| route_support::unhandled_error())?;

    let mut estimated_bytes = 0u64;
    let mut included = Vec::with_capacity(selected_specs.len());
    for spec in selected_specs {
        estimated_bytes =
            estimated_bytes.saturating_add(estimate_contributor_bytes(root, spec, &context));
        included.push(Value::String(spec.id.to_string()));
    }
    let requires_frontend = payload
        .get("requiresFrontendState")
        .map(python_truthy)
        .unwrap_or(true);
    let coverage = coverage_from_plan(&plan);
    let exclusions: Vec<Value> = context
        .exclusions()
        .into_iter()
        .map(Value::String)
        .collect();

    let state = json!({
        "backupId": backup_id,
        "phase": "preparing",
        "createdAt": created_at,
        "context": context.to_json(),
        "protection": protection,
        "requiresFrontendState": requires_frontend,
        "estimatedBytes": estimated_bytes,
        "included": included,
        "excluded": exclusions,
        "contributorPlan": plan,
        "coverage": coverage,
    });
    write_atomic_json(&session_dir.join("session.json"), &state)?;
    let mut response = state.as_object().cloned().unwrap_or_default();
    response.insert("ok".into(), Value::Bool(true));
    Ok(Value::Object(response))
}

fn put_backup_secret(
    root: &Path,
    backup_id: &str,
    payload: &Map<String, Value>,
) -> Result<Value, AppError> {
    let _ = load_session(root, backup_id)?;
    let kind = payload
        .get("kind")
        .filter(|value| !value.is_null())
        .map(value_str)
        .unwrap_or_default();
    let secret = payload
        .get("secret")
        .filter(|value| !value.is_null())
        .map(value_str)
        .unwrap_or_default();
    put_secret(backup_id, &kind, &secret)
}

fn put_restore_secret(
    root: &Path,
    restore_id: &str,
    payload: &Map<String, Value>,
) -> Result<Value, AppError> {
    let _ = restore_root(root, restore_id)?;
    let kind = payload
        .get("kind")
        .filter(|value| !value.is_null())
        .map(value_str)
        .unwrap_or_default();
    let secret = payload
        .get("secret")
        .filter(|value| !value.is_null())
        .map(value_str)
        .unwrap_or_default();
    put_secret(restore_id, &kind, &secret)
}

fn contains_forbidden_key(value: &Value) -> bool {
    match value {
        Value::Object(map) => map.iter().any(|(key, val)| {
            FORBIDDEN_FRONTEND_KEYS.iter().any(|bad| bad == key) || contains_forbidden_key(val)
        }),
        Value::Array(items) => items.iter().any(contains_forbidden_key),
        _ => false,
    }
}

fn check_json_depth(value: &Value, depth: usize) -> Result<(), AppError> {
    if depth > MAX_JSON_DEPTH {
        return Err(AppError::invalid_payload(
            "Backup JSON is too deeply nested",
        ));
    }
    match value {
        Value::Object(map) => {
            for item in map.values() {
                check_json_depth(item, depth + 1)?;
            }
        }
        Value::Array(items) => {
            for item in items {
                check_json_depth(item, depth + 1)?;
            }
        }
        _ => {}
    }
    Ok(())
}

fn validate_frontend_envelope(envelope: &Map<String, Value>) -> Result<String, AppError> {
    let version_ok = envelope.get("schemaVersion").and_then(|value| match value {
        Value::Number(num) => num.as_i64(),
        _ => None,
    }) == Some(FRONTEND_SCHEMA_VERSION);
    if !version_ok {
        return Err(AppError::invalid_payload(
            "Unsupported frontend backup schema",
        ));
    }
    let as_value = Value::Object(envelope.clone());
    if contains_forbidden_key(&as_value) {
        return Err(AppError {
            message: "Frontend backup contains ephemeral identity or credentials".to_string(),
            code: codes::SENSITIVE_CONTENT,
            status: 400,
        });
    }
    let mut body = Map::new();
    for (key, value) in envelope {
        if key != "digest" {
            body.insert(key.clone(), value.clone());
        }
    }
    let expected = sha256_compact_json(&Value::Object(body));
    let supplied = envelope
        .get("digest")
        .filter(|value| python_truthy(value))
        .map(value_str)
        .unwrap_or_default();
    if supplied.is_empty() || supplied != expected {
        return Err(AppError::invalid_payload(
            "Frontend backup digest is invalid",
        ));
    }
    check_json_depth(&as_value, 0)?;
    Ok(supplied)
}

fn put_frontend_state(
    root: &Path,
    backup_id: &str,
    envelope: &Map<String, Value>,
) -> Result<Value, AppError> {
    let mut session = load_session(root, backup_id)?;
    let phase = session
        .get("phase")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if phase != "preparing" && phase != "quiescing" {
        return Err(AppError {
            message: "Backup no longer accepts frontend state".to_string(),
            code: codes::INVALID_REQUEST,
            status: 409,
        });
    }
    let digest = validate_frontend_envelope(envelope)?;
    let session_dir = root.join(".backups").join("sessions").join(backup_id);
    write_atomic_json(
        &session_dir.join("frontend.json"),
        &Value::Object(envelope.clone()),
    )?;
    session.insert("frontendStateReceived".into(), Value::Bool(true));
    write_atomic_json(&session_dir.join("session.json"), &Value::Object(session))?;
    Ok(json!({
        "ok": true,
        "backupId": backup_id,
        "digest": digest,
    }))
}

fn delete_backup(root: &Path, backup_id: &str) -> Result<Value, AppError> {
    let session = load_session(root, backup_id)?;
    let backup_dir = root.join(".backups");
    let raw_path = session
        .get("path")
        .filter(|value| !value.is_null())
        .map(value_str)
        .unwrap_or_default();
    if !raw_path.is_empty() {
        let candidate = PathBuf::from(&raw_path);
        let path = if candidate.is_absolute() {
            candidate
        } else {
            root.join(candidate)
        };
        if path.is_file() {
            if let (Some(parent), Ok(resolved_backup_dir)) =
                (path.parent(), fs::canonicalize(&backup_dir))
            {
                if let Ok(resolved_parent) = fs::canonicalize(parent) {
                    if resolved_parent == resolved_backup_dir {
                        let _ = fs::remove_file(&path);
                    }
                }
            }
        }
    }
    clear_secret(backup_id);
    let session_dir = backup_dir.join("sessions").join(backup_id);
    let _ = fs::remove_dir_all(session_dir);
    Ok(json!({"ok": true, "deleted": true}))
}

fn delete_restore(root: &Path, restore_id: &str) -> Result<Value, AppError> {
    let dir = restore_root(root, restore_id)?;
    let meta_path = ["transaction.json", "plan.json", "upload.json"]
        .into_iter()
        .map(|name| dir.join(name))
        .find(|path| path.is_file())
        .ok_or_else(|| AppError::invalid_payload("Restore metadata is unavailable"))?;
    let metadata = read_json_object(&meta_path)?;
    let phase = metadata
        .get("phase")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if !matches!(
        phase,
        "locked" | "inspected" | "complete" | "rolled-back" | "failed"
    ) {
        return Err(AppError {
            message: "Active restore records cannot be deleted".to_string(),
            code: codes::INVALID_REQUEST,
            status: 409,
        });
    }
    if let Ok(Some(fence)) = mutation_gate::read_fence(root) {
        if fence.get("restoreId").and_then(Value::as_str) == Some(restore_id) {
            return Err(AppError {
                message: "Restore is still referenced by the active fence".to_string(),
                code: codes::INVALID_REQUEST,
                status: 409,
            });
        }
    }
    clear_secret(restore_id);
    fs::remove_dir_all(&dir).map_err(|_| route_support::unhandled_error())?;
    Ok(json!({"ok": true, "deleted": true}))
}

fn cleanup_restores(root: &Path) -> Result<Value, AppError> {
    let now = now_unix_secs_f64();
    let listed = list_restores(root)?;
    let items = listed
        .get("restores")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let mut deleted = Vec::new();
    for item in items {
        let Some(obj) = item.as_object() else {
            continue;
        };
        let phase = obj.get("phase").and_then(Value::as_str).unwrap_or_default();
        if !matches!(phase, "complete" | "rolled-back" | "failed") {
            continue;
        }
        let retention_days: f64 = if phase == "complete" { 7.0 } else { 30.0 };
        let timestamp = ["completedAt", "rolledBackAt", "updatedAt", "createdAt"]
            .into_iter()
            .find_map(|key| {
                obj.get(key)
                    .filter(|value| python_truthy(value))
                    .map(value_str)
            });
        let Some(ts) = timestamp else {
            continue;
        };
        let Ok(Some(epoch)) = parse_python_iso(&ts) else {
            continue;
        };
        let age_secs = now - (epoch as f64);
        if age_secs < retention_days * 86_400.0 {
            continue;
        }
        let Some(restore_id) = obj.get("restoreId").and_then(Value::as_str) else {
            continue;
        };
        if validate_restore_id(restore_id).is_err() {
            continue;
        }
        if delete_restore(root, restore_id).is_ok() {
            deleted.push(Value::String(restore_id.to_string()));
        }
    }
    Ok(json!({"ok": true, "deleted": deleted}))
}
