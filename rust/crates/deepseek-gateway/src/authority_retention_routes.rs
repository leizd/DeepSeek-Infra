//! Read-only Authority retention policy: `GET /api/workspace/authority/retention/policy` (S2b).
//!
//! The original reads `<root>/.backup-authority-retention/policy.json` and
//! normalizes it on every request; a missing, unreadable or unparseable file
//! answers the defaults. This port reproduces that normalization exactly,
//! including the two asymmetric branches:
//!
//! * a *falsy* stored JSON value (`[]`, `""`, `0`, `null`, `false`) reaches
//!   `normalize_retention_policy(payload or {})` as `{}` and answers the
//!   defaults, while
//! * a *truthy* non-object (`[1, 2]`, `"text"`, `7`) is the documented
//!   `400 Retention policy must be a JSON object`.
//!
//! A stored object whose fields do not validate is the original's `400` with the
//! field-specific message, so a hand-edited policy is never silently ignored.
//! Nothing is created: the directory, the policy file and the default file all
//! stay absent when they were absent.

use std::collections::BTreeMap;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};

use axum::body::Bytes;
use axum::extract::OriginalUri;
use axum::http::{HeaderMap, Method, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use axum::{Json, Router};
use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::authority_retention::{
    BuildCheckpointInput, REASON_ACTIVE_RESTORE_SESSION, REASON_CROSS_REPLICA_FORK,
    REASON_DIGEST_MISMATCH, REASON_DR_DRILL_RUNNING, REASON_FORMAL_TRUTH_STALE,
    REASON_GC_UNFINISHED, REASON_INSUFFICIENT_HISTORY, REASON_REPLICA_LAG,
    REASON_UNKNOWN_MUTATION_TYPE, STATE_COMMITTED, STATE_EXECUTING, STATE_FAILED, STATE_READY,
    STATE_VERIFYING, build_authority_retention_checkpoint, json_i64_or, normalize_retention_policy,
    python_exact_int, strip_secrets, verify_authority_chain, verify_authority_checkpoint_integrity,
    verify_compaction,
};
use deepseek_policy::backup_mirror::{dumps_indent_two, fsync_dir, now_iso, sha256_hex};
use deepseek_policy::core_utils::python_truthy;
use deepseek_policy::entropy::{Entropy, SystemEntropy};
use deepseek_policy::python_json::value_str;
use rusqlite::{Connection, OptionalExtension};
use serde_json::{Map, Value, json};

use crate::backup_catalog_routes::open_readonly_sqlite;
use crate::route_support;

const AUTHORITY_RETENTION_DOMAIN: &str = "authority_retention_store";
const AUTHORITY_RETENTION_WRITE_NOT_OWNED: &str = "NATIVE_AUTHORITY_RETENTION_WRITE_NOT_OWNED";
const ENV_AUTHORITY_MODE: &str = "DEEPSEEK_CONTROL_AUTHORITY_MODE";
const ENV_ANCHOR_ROOTS: &str = "DEEPSEEK_CONTROL_AUTHORITY_ANCHOR_ROOTS";
const ENV_BOOTSTRAP_LOCATORS: &str = "DEEPSEEK_CONTROL_AUTHORITY_BOOTSTRAP_LOCATORS";

const TERMINAL_RESTORE_PHASES: [&str; 5] =
    ["completed", "verified", "failed", "cancelled", "abandoned"];
const KNOWN_MUTATION_KINDS: [&str; 12] = [
    "authority-bootstrap",
    "target-change",
    "policy-update",
    "security-event",
    "formal-truth-failure",
    "mutation",
    "promotion",
    "drain",
    "placement",
    "checkpoint",
    "retention",
    "receipt-ack",
];

pub fn router() -> Router {
    Router::new()
        .route(
            "/api/workspace/authority/retention/policy",
            any(policy_response),
        )
        .route(
            "/api/workspace/authority/history-snapshot",
            any(history_snapshot_response),
        )
        .route(
            "/api/workspace/authority/retention/explain",
            any(explain_response),
        )
        .route(
            "/api/workspace/authority/retention/plan",
            any(plan_response),
        )
        .route(
            "/api/workspace/authority/retention/compact",
            any(compact_response),
        )
}

async fn policy_response(
    method: Method,
    headers: HeaderMap,
    _uri: OriginalUri,
    body: Bytes,
) -> Response {
    match method {
        Method::GET | Method::HEAD => {
            let joined = tokio::task::spawn_blocking(policy_body).await;
            match joined {
                Ok(Ok(_)) if method == Method::HEAD => StatusCode::OK.into_response(),
                Ok(Ok(body)) => Json(body).into_response(),
                Ok(Err(error)) => route_support::app_error_response(error),
                Err(_) if method == Method::HEAD => {
                    StatusCode::INTERNAL_SERVER_ERROR.into_response()
                }
                Err(_) => route_support::app_error_response(route_support::unhandled_error()),
            }
        }
        // `POST` on this exact path is the original's policy *mutation*
        // (`put_authority_retention_policy`), which this slice does not
        // implement. Forwarding keeps the pre-existing fail-closed behaviour
        // instead of impersonating a write with the catch-all 404.
        Method::POST => post_policy_response(&headers, &body).await,
        Method::OPTIONS => route_support::options_response(),
        _ => route_support::method_not_allowed(),
    }
}

fn policy_body() -> Result<Value, AppError> {
    normalize_retention_policy(&stored_policy()?)
}

fn policy_path() -> PathBuf {
    crate::data_routes::workspace_root()
        .join(".backup-authority-retention")
        .join("policy.json")
}

/// `json.loads(path.read_text())` with the original's two fallbacks: an `OSError`
/// or a `JSONDecodeError` answers the defaults.
fn stored_policy() -> Result<Value, AppError> {
    let path = policy_path();
    if !path.is_file() {
        return Ok(Value::Null);
    }
    let Ok(text) = std::fs::read_to_string(&path) else {
        return Ok(Value::Null);
    };
    Ok(serde_json::from_str(&text).unwrap_or(Value::Null))
}

fn retention_dir() -> PathBuf {
    crate::data_routes::workspace_root().join(".backup-authority-retention")
}

fn atomic_write_json(path: &Path, payload: &Value) -> Result<(), AppError> {
    let parent = path.parent().ok_or_else(route_support::unhandled_error)?;
    fs::create_dir_all(parent).map_err(|_| route_support::unhandled_error())?;
    let name = path
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("data.json");
    let tmp = parent.join(format!(".{name}.{}.tmp", std::process::id()));
    let content = format!("{}\n", dumps_indent_two(payload));
    {
        let mut handle = OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .open(&tmp)
            .map_err(|_| route_support::unhandled_error())?;
        handle
            .write_all(content.as_bytes())
            .map_err(|_| route_support::unhandled_error())?;
        handle
            .flush()
            .map_err(|_| route_support::unhandled_error())?;
        handle
            .sync_all()
            .map_err(|_| route_support::unhandled_error())?;
    }
    fs::rename(&tmp, path).map_err(|_| route_support::unhandled_error())?;
    fsync_dir(parent);
    Ok(())
}

async fn post_policy_response(headers: &HeaderMap, body: &[u8]) -> Response {
    let headers = headers.clone();
    let bytes = body.to_vec();
    let joined = tokio::task::spawn_blocking(move || handle_post_policy(&headers, &bytes)).await;
    match joined {
        Ok(Ok(value)) => Json(value).into_response(),
        Ok(Err(error)) => route_support::app_error_response(error),
        Err(_) => route_support::app_error_response(route_support::unhandled_error()),
    }
}

fn handle_post_policy(headers: &HeaderMap, body_bytes: &[u8]) -> Result<Value, AppError> {
    let obj = route_support::read_json_object(headers, body_bytes, 64_000)?;
    let normalized = normalize_retention_policy(&Value::Object(obj))?;
    route_support::require_native_store(
        AUTHORITY_RETENTION_DOMAIN,
        AUTHORITY_RETENTION_WRITE_NOT_OWNED,
    )?;
    atomic_write_json(&policy_path(), &normalized)?;
    Ok(normalized)
}

async fn history_snapshot_response(method: Method) -> Response {
    if method != Method::GET && method != Method::HEAD {
        return route_support::unallowed_api_method(method, Method::POST);
    }
    let joined = tokio::task::spawn_blocking(handle_history_snapshot).await;
    match joined {
        Ok(Ok(_)) if method == Method::HEAD => StatusCode::OK.into_response(),
        Ok(Ok(value)) => Json(value).into_response(),
        Ok(Err(error)) => route_support::app_error_response(error),
        Err(_) if method == Method::HEAD => StatusCode::INTERNAL_SERVER_ERROR.into_response(),
        Err(_) => route_support::app_error_response(route_support::unhandled_error()),
    }
}

async fn explain_response(method: Method, headers: HeaderMap, body: Bytes) -> Response {
    if method != Method::POST {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    let joined = tokio::task::spawn_blocking(move || handle_explain(&headers, &body)).await;
    match joined {
        Ok(Ok(value)) => Json(value).into_response(),
        Ok(Err(error)) => route_support::app_error_response(error),
        Err(_) => route_support::app_error_response(route_support::unhandled_error()),
    }
}

async fn plan_response(method: Method, headers: HeaderMap, body: Bytes) -> Response {
    if method != Method::POST {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    let joined = tokio::task::spawn_blocking(move || handle_plan(&headers, &body)).await;
    match joined {
        Ok(Ok(value)) => Json(value).into_response(),
        Ok(Err(error)) => route_support::app_error_response(error),
        Err(_) => route_support::app_error_response(route_support::unhandled_error()),
    }
}

async fn compact_response(method: Method, headers: HeaderMap, body: Bytes) -> Response {
    if method != Method::POST {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    let joined = tokio::task::spawn_blocking(move || handle_compact(&headers, &body)).await;
    match joined {
        Ok(Ok(value)) => Json(value).into_response(),
        Ok(Err(error)) => route_support::app_error_response(error),
        Err(_) => route_support::app_error_response(route_support::unhandled_error()),
    }
}

#[derive(Clone)]
struct AuthorityMutationRow {
    mutation_id: String,
    generation: i64,
    digest: String,
    kind: String,
    checkpoint: Option<Value>,
}

struct LoadedControlState {
    current_generation: i64,
    head_digest: String,
    mutations: Vec<AuthorityMutationRow>,
    latest_checkpoint: Option<Value>,
    active_restores: Vec<String>,
    gc_unfinished: bool,
    dr_drill_running: bool,
    replica_roots: Vec<PathBuf>,
    formal_truth_stale: Option<String>,
}

fn new_short_id(prefix: &str) -> String {
    let hex = SystemEntropy
        .new_id()
        .unwrap_or_else(|_| "000000000000".to_string());
    let short: String = hex.chars().take(12).collect();
    format!("{prefix}_{short}")
}

fn resolve_replica_roots() -> Vec<PathBuf> {
    let mut roots = Vec::new();
    if let Ok(raw) = std::env::var(ENV_ANCHOR_ROOTS) {
        let trimmed = raw.trim();
        if !trimmed.is_empty() {
            if let Ok(Value::Array(items)) = serde_json::from_str::<Value>(trimmed) {
                for item in items {
                    let text = value_str(&item).trim().to_string();
                    if !text.is_empty() {
                        roots.push(PathBuf::from(text));
                    }
                }
            } else {
                for part in trimmed.split([',', ';']) {
                    let text = part.trim();
                    if !text.is_empty() {
                        roots.push(PathBuf::from(text));
                    }
                }
            }
        }
    }
    if roots.is_empty() {
        if let Ok(raw) = std::env::var(ENV_BOOTSTRAP_LOCATORS) {
            let trimmed = raw.trim();
            if !trimmed.is_empty() {
                if let Ok(Value::Array(items)) = serde_json::from_str::<Value>(trimmed) {
                    for item in items {
                        if let Some(root_str) = item
                            .as_object()
                            .and_then(|o| o.get("root").or_else(|| o.get("path")))
                            .and_then(Value::as_str)
                        {
                            if !root_str.trim().is_empty() {
                                roots.push(PathBuf::from(root_str.trim()));
                            }
                        } else if let Some(s) = item.as_str() {
                            if !s.trim().is_empty() {
                                roots.push(PathBuf::from(s.trim()));
                            }
                        }
                    }
                }
            }
        }
    }
    roots
}

fn check_formal_truth_stale(workspace: &Path) -> Option<String> {
    let mode = std::env::var(ENV_AUTHORITY_MODE)
        .unwrap_or_else(|_| "replicated".to_string())
        .trim()
        .to_lowercase();
    if mode == "local-only" {
        return None;
    }
    let path = workspace.join(".backup-control").join("formal-truth.json");
    if !path.is_file() {
        return None;
    }
    let Ok(text) = fs::read_to_string(&path) else {
        return Some("Authority formal truth state is unreadable".to_string());
    };
    let Ok(Value::Object(obj)) = serde_json::from_str::<Value>(&text) else {
        return Some("Authority formal truth state is invalid".to_string());
    };
    let verdict = obj
        .get("verdict")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();
    if !verdict.is_empty() && verdict != "active" && verdict != "HEALTHY" {
        return Some(format!("Authority formal truth verdict is {verdict}"));
    }
    None
}

fn load_latest_retention_checkpoint() -> Option<Value> {
    let dir = retention_dir().join("checkpoints");
    let Ok(entries) = fs::read_dir(&dir) else {
        return None;
    };
    let mut paths: Vec<PathBuf> = entries
        .flatten()
        .map(|entry| entry.path())
        .filter(|path| path.extension().and_then(|e| e.to_str()) == Some("json"))
        .collect();
    paths.sort();
    for path in paths.into_iter().rev() {
        let Ok(text) = fs::read_to_string(&path) else {
            continue;
        };
        let Ok(Value::Object(obj)) = serde_json::from_str::<Value>(&text) else {
            continue;
        };
        if obj
            .get("checkpointGeneration")
            .and_then(Value::as_i64)
            .is_some()
        {
            return Some(Value::Object(obj));
        }
    }
    None
}

fn list_active_restores(workspace: &Path) -> Vec<String> {
    let dir = workspace.join(".restore-staging");
    let Ok(entries) = fs::read_dir(&dir) else {
        return Vec::new();
    };
    let mut active = Vec::new();
    for entry in entries.flatten() {
        let session = entry.path();
        if !session.is_dir() {
            continue;
        }
        let meta = session.join("remote-fetch.json");
        if !meta.is_file() {
            continue;
        }
        let Ok(text) = fs::read_to_string(&meta) else {
            continue;
        };
        let Ok(Value::Object(obj)) = serde_json::from_str::<Value>(&text) else {
            continue;
        };
        let phase = obj
            .get("phase")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        if !TERMINAL_RESTORE_PHASES.contains(&phase.as_str()) {
            let rid = obj
                .get("restoreId")
                .filter(|v| python_truthy(v))
                .map(value_str)
                .unwrap_or_else(|| {
                    session
                        .file_name()
                        .and_then(|n| n.to_str())
                        .unwrap_or_default()
                        .to_string()
                });
            active.push(rid);
        }
    }
    active.sort();
    active
}

fn load_control_state() -> Result<LoadedControlState, AppError> {
    let workspace = crate::data_routes::workspace_root();
    let db = workspace.join(".backup-control").join("control.sqlite3");

    let mut current_generation = 0i64;
    let mut head_digest = String::new();
    let mut mutations = Vec::new();
    let mut gc_unfinished = false;

    if let Some(conn) = open_readonly_sqlite(&db)? {
        let has_head: Option<String> = conn
            .query_row(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'control_authority_head'",
                [],
                |row| row.get(0),
            )
            .optional()
            .map_err(|_| route_support::unhandled_error())?;
        if has_head.is_some() {
            let head_row: Option<(i64, String)> = conn
                .query_row(
                    "SELECT authority_generation, authority_digest FROM control_authority_head WHERE id = 1",
                    [],
                    |row| Ok((row.get(0)?, row.get(1)?)),
                )
                .optional()
                .map_err(|_| route_support::unhandled_error())?;
            if let Some((head_gen, dig)) = head_row {
                current_generation = head_gen;
                head_digest = dig;
            }
        }

        let has_mutations: Option<String> = conn
            .query_row(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'control_authority_mutations'",
                [],
                |row| row.get(0),
            )
            .optional()
            .map_err(|_| route_support::unhandled_error())?;
        if has_mutations.is_some() {
            let mut stmt = conn
                .prepare(
                    "SELECT mutation_id, authority_generation, authority_digest, kind, checkpoint_json
                     FROM control_authority_mutations
                     ORDER BY authority_generation ASC",
                )
                .map_err(|_| route_support::unhandled_error())?;
            let rows = stmt
                .query_map([], |row| {
                    let mutation_id: String = row.get(0)?;
                    let generation: i64 = row.get(1)?;
                    let digest: String = row.get(2)?;
                    let kind: String = row.get(3)?;
                    let ckpt_raw: Option<String> = row.get(4)?;
                    Ok((mutation_id, generation, digest, kind, ckpt_raw))
                })
                .map_err(|_| route_support::unhandled_error())?;
            for item in rows {
                let (mutation_id, generation, digest, kind, ckpt_raw) =
                    item.map_err(|_| route_support::unhandled_error())?;
                let checkpoint = ckpt_raw
                    .as_deref()
                    .and_then(|s| serde_json::from_str::<Value>(s).ok())
                    .map(|val| strip_secrets(&val));
                mutations.push(AuthorityMutationRow {
                    mutation_id,
                    generation,
                    digest,
                    kind,
                    checkpoint,
                });
            }
            if current_generation == 0 {
                if let Some(last) = mutations.last() {
                    current_generation = last.generation;
                    head_digest = last.digest.clone();
                }
            }
        }

        let has_gc: Option<String> = conn
            .query_row(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'target_receipt_mutations'",
                [],
                |row| row.get(0),
            )
            .optional()
            .map_err(|_| route_support::unhandled_error())?;
        if has_gc.is_some() {
            let pending: i64 = conn
                .query_row(
                    "SELECT COUNT(*) FROM target_receipt_mutations WHERE status NOT IN ('committed', 'aborted', 'retired')",
                    [],
                    |row| row.get(0),
                )
                .unwrap_or(0);
            if pending > 0 {
                gc_unfinished = true;
            }
        }
    }

    let dr_drill_running = workspace
        .join(".backup-dr")
        .join("drill-lock.json")
        .is_file();

    Ok(LoadedControlState {
        current_generation,
        head_digest,
        mutations,
        latest_checkpoint: load_latest_retention_checkpoint(),
        active_restores: list_active_restores(&workspace),
        gc_unfinished,
        dr_drill_running,
        replica_roots: resolve_replica_roots(),
        formal_truth_stale: check_formal_truth_stale(&workspace),
    })
}

fn check_replica_roots(
    state: &LoadedControlState,
    reasons: &mut Vec<Value>,
) -> BTreeMap<String, Value> {
    let mut coverage = BTreeMap::new();
    for (idx, root) in state.replica_roots.iter().enumerate() {
        let rep_id = root
            .file_name()
            .and_then(|n| n.to_str())
            .filter(|s| !s.is_empty())
            .map(ToString::to_string)
            .unwrap_or_else(|| format!("replica_{idx}"));
        let head_file = root.join("control").join("authority").join("head.json");
        let ckpt_dir = root.join("control").join("authority").join("checkpoints");
        if !head_file.is_file() {
            reasons.push(json!({
                "code": REASON_REPLICA_LAG,
                "replicaId": rep_id,
                "message": format!("Replica {rep_id} authority head is missing"),
            }));
            continue;
        }
        let Ok(head_text) = fs::read_to_string(&head_file) else {
            reasons.push(json!({
                "code": REASON_REPLICA_LAG,
                "replicaId": rep_id,
                "message": format!("Replica {rep_id} authority head is unreadable"),
            }));
            continue;
        };
        let Ok(head_val) = serde_json::from_str::<Value>(&head_text) else {
            reasons.push(json!({
                "code": REASON_CROSS_REPLICA_FORK,
                "replicaId": rep_id,
                "message": format!("Replica {rep_id} authority head is corrupt"),
            }));
            continue;
        };
        let rep_gen = json_i64_or(head_val.get("authorityGeneration"), 0);
        let rep_dig = head_val
            .get("digest")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();

        if ckpt_dir.is_dir() {
            if let Ok(entries) = fs::read_dir(&ckpt_dir) {
                let mut rep_ckpts = Vec::new();
                for entry in entries.flatten() {
                    let path = entry.path();
                    if path.extension().and_then(|e| e.to_str()) == Some("json") {
                        if let Ok(text) = fs::read_to_string(&path) {
                            if let Ok(val) = serde_json::from_str::<Value>(&text) {
                                rep_ckpts.push(val);
                            }
                        }
                    }
                }
                if !rep_ckpts.is_empty() && verify_authority_chain(&rep_ckpts).is_err() {
                    reasons.push(json!({
                        "code": REASON_CROSS_REPLICA_FORK,
                        "replicaId": rep_id,
                        "message": format!("Replica {rep_id} authority checkpoint chain forked or invalid"),
                    }));
                }
                for rckpt in &rep_ckpts {
                    let rgen = json_i64_or(rckpt.get("authorityGeneration"), 0);
                    let rdig = rckpt
                        .get("digest")
                        .filter(|v| python_truthy(v))
                        .map(value_str)
                        .unwrap_or_default();
                    if let Some(local) = state.mutations.iter().find(|m| m.generation == rgen) {
                        if local.digest != rdig {
                            reasons.push(json!({
                                "code": REASON_CROSS_REPLICA_FORK,
                                "replicaId": rep_id,
                                "generation": rgen,
                                "message": format!("Replica {rep_id} diverged at generation {rgen}"),
                            }));
                            break;
                        }
                    }
                }
            }
        }

        if rep_gen < state.current_generation {
            reasons.push(json!({
                "code": REASON_REPLICA_LAG,
                "replicaId": rep_id,
                "replicaGeneration": rep_gen,
                "currentGeneration": state.current_generation,
                "message": format!(
                    "Replica {rep_id} is lagging at generation {rep_gen} (current {})",
                    state.current_generation
                ),
            }));
        }

        coverage.insert(
            rep_id,
            json!({
                "generation": rep_gen,
                "digest": rep_dig,
            }),
        );
    }
    coverage
}

struct EvaluatedAuthorityRetention {
    target_generation: i64,
    tail_retained_count: i64,
    tail_start_generation: i64,
    allowed: bool,
    reasons: Vec<Value>,
    eligible_prune_generations: Vec<i64>,
    retained_special_generations: Vec<i64>,
    dependencies: Map<String, Value>,
    replica_coverage: BTreeMap<String, Value>,
}

fn evaluate_retention(
    state: &LoadedControlState,
    policy: &Value,
    requested_target_gen: Option<i64>,
) -> EvaluatedAuthorityRetention {
    let min_gens = json_i64_or(policy.get("minimumGenerations"), 100);
    let current_gen = state.current_generation;
    let target_gen = match requested_target_gen {
        Some(target) => target,
        None => (current_gen - min_gens).max(1),
    };
    let tail_retained_count = if current_gen <= target_gen {
        0
    } else {
        current_gen - target_gen
    };

    let mut reasons = Vec::new();
    if let Some(ref stale_msg) = state.formal_truth_stale {
        reasons.push(json!({
            "code": REASON_FORMAL_TRUTH_STALE,
            "message": stale_msg,
        }));
    }
    if current_gen <= min_gens {
        reasons.push(json!({
            "code": REASON_INSUFFICIENT_HISTORY,
            "currentGeneration": current_gen,
            "minimumGenerations": min_gens,
            "message": format!(
                "Current generation ({current_gen}) does not exceed minimum retained tail ({min_gens})"
            ),
        }));
    } else if requested_target_gen.is_some()
        && (target_gen < 1 || target_gen >= current_gen || current_gen - target_gen < min_gens)
    {
        reasons.push(json!({
            "code": REASON_INSUFFICIENT_HISTORY,
            "currentGeneration": current_gen,
            "minimumGenerations": min_gens,
            "targetGeneration": target_gen,
            "message": format!(
                "Target generation ({target_gen}) leaves fewer than minimumGenerations ({min_gens}) tail entries"
            ),
        }));
    }

    if !state.active_restores.is_empty() {
        reasons.push(json!({
            "code": REASON_ACTIVE_RESTORE_SESSION,
            "restoreIds": state.active_restores,
            "message": format!(
                "Active restore session(s) block compaction: {}",
                state.active_restores.join(", ")
            ),
        }));
    }
    if state.dr_drill_running {
        reasons.push(json!({
            "code": REASON_DR_DRILL_RUNNING,
            "message": "A DR verification drill is currently running",
        }));
    }
    if state.gc_unfinished {
        reasons.push(json!({
            "code": REASON_GC_UNFINISHED,
            "message": "Target receipt index GC has unfinished mutations",
        }));
    }

    let replica_coverage = check_replica_roots(state, &mut reasons);

    for row in &state.mutations {
        if !KNOWN_MUTATION_KINDS.contains(&row.kind.as_str()) {
            reasons.push(json!({
                "code": REASON_UNKNOWN_MUTATION_TYPE,
                "generation": row.generation,
                "kind": row.kind,
                "message": format!("Unknown mutation kind '{}' at generation {}", row.kind, row.generation),
            }));
            break;
        }
        if let Some(ref ckpt) = row.checkpoint {
            if verify_authority_checkpoint_integrity(ckpt).is_err() {
                reasons.push(json!({
                    "code": REASON_DIGEST_MISMATCH,
                    "generation": row.generation,
                    "message": format!("Checkpoint digest mismatch at generation {}", row.generation),
                }));
                break;
            }
        }
    }

    let keep_classes: Vec<String> = policy
        .get("keepMutationClasses")
        .and_then(Value::as_array)
        .map(|arr| arr.iter().map(value_str).collect())
        .unwrap_or_default();

    let mut eligible_prune = Vec::new();
    let mut retained_special = Vec::new();
    let mut dependencies = Map::new();
    if reasons.is_empty() {
        for row in &state.mutations {
            if row.generation > target_gen {
                continue;
            }
            if keep_classes.contains(&row.kind) {
                retained_special.push(row.generation);
                dependencies.insert(
                    row.generation.to_string(),
                    json!([{
                        "type": "mutation_journal",
                        "ref": row.mutation_id,
                        "detail": format!("kind:{}", row.kind),
                    }]),
                );
            } else {
                eligible_prune.push(row.generation);
            }
        }
    }

    let allowed = reasons.is_empty();
    EvaluatedAuthorityRetention {
        target_generation: target_gen,
        tail_retained_count,
        tail_start_generation: target_gen + 1,
        allowed,
        reasons,
        eligible_prune_generations: eligible_prune,
        retained_special_generations: retained_special,
        dependencies,
        replica_coverage,
    }
}

fn explanation_json(state: &LoadedControlState, eval: &EvaluatedAuthorityRetention) -> Value {
    json!({
        "allowed": eval.allowed,
        "reasons": eval.reasons,
        "currentGeneration": state.current_generation,
        "targetGeneration": eval.target_generation,
        "tailRetainedCount": eval.tail_retained_count,
        "headDigest": state.head_digest,
    })
}

fn plan_json(
    state: &LoadedControlState,
    policy: &Value,
    eval: &EvaluatedAuthorityRetention,
) -> Value {
    json!({
        "planId": new_short_id("ret_plan"),
        "allowed": eval.allowed,
        "reasons": eval.reasons,
        "policy": policy,
        "currentGeneration": state.current_generation,
        "headDigest": state.head_digest,
        "targetCheckpointGeneration": eval.target_generation,
        "tailStartGeneration": eval.tail_start_generation,
        "tailCount": eval.tail_retained_count,
        "eligiblePruneGenerations": eval.eligible_prune_generations,
        "retainedSpecialGenerations": eval.retained_special_generations,
        "dependencies": Value::Object(eval.dependencies.clone()),
        "createdAt": now_iso(None),
    })
}

struct ParsedAuthorityRequest {
    policy: Value,
    target_generation: Option<i64>,
    body: Map<String, Value>,
}

fn parse_optional_policy_and_target(
    headers: &HeaderMap,
    body_bytes: &[u8],
) -> Result<ParsedAuthorityRequest, AppError> {
    let body = route_support::read_json_object(headers, body_bytes, 64_000)?;
    let policy = match body.get("policy") {
        None | Some(Value::Null) => policy_body()?,
        Some(override_val) => normalize_retention_policy(override_val)?,
    };
    let target_generation = match body.get("targetGeneration") {
        None | Some(Value::Null) => None,
        Some(val) => {
            let Some(target) = python_exact_int(val) else {
                return Err(AppError {
                    message: "targetGeneration must be an integer".to_string(),
                    code: codes::INVALID_PAYLOAD,
                    status: 400,
                });
            };
            Some(target)
        }
    };
    Ok(ParsedAuthorityRequest {
        policy,
        target_generation,
        body,
    })
}

fn handle_history_snapshot() -> Result<Value, AppError> {
    let policy = policy_body()?;
    let state = load_control_state()?;
    let eval = evaluate_retention(&state, &policy, None);
    let explanation = explanation_json(&state, &eval);

    let checkpoint_gen = state
        .latest_checkpoint
        .as_ref()
        .and_then(|c| c.get("checkpointGeneration"))
        .and_then(Value::as_i64);
    let checkpoint_digest = state
        .latest_checkpoint
        .as_ref()
        .and_then(|c| c.get("headDigest"))
        .cloned()
        .unwrap_or(Value::Null);
    let last_compaction = state
        .latest_checkpoint
        .as_ref()
        .and_then(|c| c.get("createdAt"))
        .cloned()
        .unwrap_or(Value::Null);
    let oldest_retained_gen = state.mutations.first().map(|m| m.generation).unwrap_or(1);
    let status = if eval.allowed { "HEALTHY" } else { "BLOCKED" };

    Ok(json!({
        "currentGeneration": state.current_generation,
        "headDigest": state.head_digest,
        "checkpointGeneration": checkpoint_gen,
        "checkpointDigest": checkpoint_digest,
        "tailCount": eval.tail_retained_count,
        "oldestRetainedGeneration": oldest_retained_gen,
        "totalHistoryCount": state.mutations.len(),
        "lastCompaction": last_compaction,
        "retentionPolicy": policy,
        "status": status,
        "explanation": explanation,
    }))
}

fn handle_explain(headers: &HeaderMap, body_bytes: &[u8]) -> Result<Value, AppError> {
    let req = parse_optional_policy_and_target(headers, body_bytes)?;
    let state = load_control_state()?;
    let eval = evaluate_retention(&state, &req.policy, req.target_generation);
    Ok(explanation_json(&state, &eval))
}

fn handle_plan(headers: &HeaderMap, body_bytes: &[u8]) -> Result<Value, AppError> {
    let req = parse_optional_policy_and_target(headers, body_bytes)?;
    let state = load_control_state()?;
    let eval = evaluate_retention(&state, &req.policy, req.target_generation);
    Ok(plan_json(&state, &req.policy, &eval))
}

fn handle_compact(headers: &HeaderMap, body_bytes: &[u8]) -> Result<Value, AppError> {
    let req = parse_optional_policy_and_target(headers, body_bytes)?;
    route_support::require_native_store(
        AUTHORITY_RETENTION_DOMAIN,
        AUTHORITY_RETENTION_WRITE_NOT_OWNED,
    )?;

    let dry_run = req.body.get("dryRun").map(python_truthy).unwrap_or(false);
    let state = load_control_state()?;
    let eval = evaluate_retention(&state, &req.policy, req.target_generation);
    let plan = plan_json(&state, &req.policy, &eval);

    if !eval.allowed {
        let msgs: Vec<String> = eval
            .reasons
            .iter()
            .filter_map(|r| r.get("message").map(value_str))
            .collect();
        return Err(AppError {
            message: format!("Compaction blocked: {}", msgs.join("; ")),
            code: codes::INVALID_REQUEST,
            status: 409,
        });
    }

    let job_id = new_short_id("job_ret");
    if dry_run {
        return Ok(json!({
            "jobId": job_id,
            "state": STATE_READY,
            "dryRun": true,
            "plan": plan,
        }));
    }

    let target_checkpoint_gen = eval.target_generation;
    let full_history: Vec<Value> = state
        .mutations
        .iter()
        .map(|row| {
            row.checkpoint.clone().unwrap_or_else(|| {
                json!({
                    "authorityGeneration": row.generation,
                    "digest": row.digest,
                    "kind": row.kind,
                })
            })
        })
        .collect();

    let Some(target_row) = state
        .mutations
        .iter()
        .find(|row| row.generation == target_checkpoint_gen)
    else {
        return Err(AppError {
            message: format!(
                "Target checkpoint generation {target_checkpoint_gen} not found in history"
            ),
            code: codes::INTERNAL,
            status: 500,
        });
    };

    let history_start_gen = state.mutations.first().map(|m| m.generation).unwrap_or(1);
    let included: Vec<Value> = state
        .mutations
        .iter()
        .filter(|row| row.generation <= target_checkpoint_gen)
        .map(|row| {
            row.checkpoint.clone().unwrap_or_else(|| {
                json!({
                    "authorityGeneration": row.generation,
                    "digest": row.digest,
                    "kind": row.kind,
                })
            })
        })
        .collect();
    let tail_history: Vec<Value> = state
        .mutations
        .iter()
        .filter(|row| row.generation > target_checkpoint_gen)
        .map(|row| {
            row.checkpoint.clone().unwrap_or_else(|| {
                json!({
                    "authorityGeneration": row.generation,
                    "digest": row.digest,
                    "kind": row.kind,
                })
            })
        })
        .collect();

    let formal_truth_digest = sha256_hex(state.head_digest.as_bytes());
    let checkpoint = build_authority_retention_checkpoint(BuildCheckpointInput {
        checkpoint_generation: target_checkpoint_gen,
        ancestor_digest: &target_row.digest,
        head_digest: &state.head_digest,
        history_start_generation: history_start_gen,
        included_mutations: &included,
        replica_coverage: &eval.replica_coverage,
        formal_truth_digest: &formal_truth_digest,
        created_at: None,
    })?;

    let verification = verify_compaction(&checkpoint, &tail_history, &full_history)?;

    let job_path = retention_dir().join("jobs").join(format!("{job_id}.json"));
    atomic_write_json(
        &job_path,
        &json!({
            "jobId": job_id,
            "state": STATE_EXECUTING,
            "targetCheckpointGeneration": target_checkpoint_gen,
            "updatedAt": now_iso(None),
        }),
    )?;

    let ckpt_path = retention_dir()
        .join("checkpoints")
        .join(format!("{target_checkpoint_gen:016}.json"));
    atomic_write_json(&ckpt_path, &checkpoint)?;

    atomic_write_json(
        &job_path,
        &json!({
            "jobId": job_id,
            "state": STATE_VERIFYING,
            "targetCheckpointGeneration": target_checkpoint_gen,
            "updatedAt": now_iso(None),
        }),
    )?;

    let pruned_count = eval.eligible_prune_generations.len();
    if let Err(err) = prune_control_mutations(&eval.eligible_prune_generations) {
        let _ = atomic_write_json(
            &job_path,
            &json!({
                "jobId": job_id,
                "state": STATE_FAILED,
                "error": err.message,
                "updatedAt": now_iso(None),
            }),
        );
        return Err(err);
    }

    atomic_write_json(
        &job_path,
        &json!({
            "jobId": job_id,
            "state": STATE_COMMITTED,
            "targetCheckpointGeneration": target_checkpoint_gen,
            "prunedGenerationsCount": pruned_count,
            "updatedAt": now_iso(None),
        }),
    )?;

    Ok(json!({
        "jobId": job_id,
        "state": STATE_COMMITTED,
        "checkpoint": checkpoint,
        "verification": verification,
        "prunedGenerationsCount": pruned_count,
        "retentionSafety": {
            "checkpointVerified": true,
            "ancestorCoverage": true,
            "replicaAgreement": true,
            "dependencyClosure": true,
        },
    }))
}

fn prune_control_mutations(generations: &[i64]) -> Result<(), AppError> {
    if generations.is_empty() {
        return Ok(());
    }
    let db = crate::data_routes::workspace_root()
        .join(".backup-control")
        .join("control.sqlite3");
    let conn = Connection::open(&db).map_err(|e| AppError {
        message: format!("Prune failed: {e}"),
        code: codes::INTERNAL,
        status: 500,
    })?;
    conn.busy_timeout(std::time::Duration::from_secs(30))
        .map_err(|e| AppError {
            message: format!("Prune failed: {e}"),
            code: codes::INTERNAL,
            status: 500,
        })?;
    conn.execute_batch("BEGIN IMMEDIATE;")
        .map_err(|e| AppError {
            message: format!("Prune failed: {e}"),
            code: codes::INTERNAL,
            status: 500,
        })?;
    for generation in generations {
        if let Err(e) = conn.execute(
            "DELETE FROM control_authority_mutations WHERE authority_generation = ?1",
            [*generation],
        ) {
            let _ = conn.execute_batch("ROLLBACK;");
            return Err(AppError {
                message: format!("Prune failed: {e}"),
                code: codes::INTERNAL,
                status: 500,
            });
        }
    }
    conn.execute_batch("COMMIT;").map_err(|e| AppError {
        message: format!("Prune failed: {e}"),
        code: codes::INTERNAL,
        status: 500,
    })?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{normalize_retention_policy, python_exact_int};
    use serde_json::json;

    fn defaults() -> serde_json::Value {
        // `sorted({...})` of the declared defaults: the source order is
        // authority-bootstrap, target-change, security-event,
        // formal-truth-failure, but the answer is alphabetical.
        json!({
            "retentionPolicyVersion": 1,
            "minimumGenerations": 100,
            "minimumAgeDays": 30,
            "keepMutationClasses": [
                "authority-bootstrap",
                "formal-truth-failure",
                "security-event",
                "target-change"
            ],
            "checkpointInterval": 10000,
        })
    }

    #[test]
    fn empty_and_falsy_payloads_answer_the_defaults() {
        assert_eq!(
            normalize_retention_policy(&json!(null)).unwrap(),
            defaults()
        );
        assert_eq!(normalize_retention_policy(&json!({})).unwrap(), defaults());
        assert_eq!(normalize_retention_policy(&json!([])).unwrap(), defaults());
        assert_eq!(normalize_retention_policy(&json!("")).unwrap(), defaults());
        assert_eq!(normalize_retention_policy(&json!(0)).unwrap(), defaults());
        assert_eq!(
            normalize_retention_policy(&json!(false)).unwrap(),
            defaults()
        );
    }

    #[test]
    fn truthy_non_objects_are_the_documented_400() {
        for payload in [json!([1, 2]), json!("text"), json!(7), json!(true)] {
            let error = normalize_retention_policy(&payload).unwrap_err();
            assert_eq!(error.message, "Retention policy must be a JSON object");
            assert_eq!(error.status, 400);
            assert_eq!(error.code, "invalid_payload");
        }
    }

    #[test]
    fn field_validation_matches_the_original_messages() {
        let cases = [
            (
                json!({"retentionPolicyVersion": 2}),
                "Unsupported retention policy version: 2",
            ),
            (
                json!({"retentionPolicyVersion": "1"}),
                "Unsupported retention policy version: 1",
            ),
            (
                json!({"retentionPolicyVersion": null}),
                "Unsupported retention policy version: None",
            ),
            (
                json!({"retentionPolicyVersion": false}),
                "Unsupported retention policy version: False",
            ),
            (
                json!({"retentionPolicyVersion": 0}),
                "Unsupported retention policy version: 0",
            ),
            (
                json!({"retentionPolicyVersion": []}),
                "Unsupported retention policy version: []",
            ),
            (
                json!({"minimumGenerations": true}),
                "minimumGenerations must be an integer >= 1",
            ),
            (
                json!({"minimumGenerations": 0}),
                "minimumGenerations must be an integer >= 1",
            ),
            (
                json!({"minimumGenerations": 1.0}),
                "minimumGenerations must be an integer >= 1",
            ),
            (
                json!({"minimumGenerations": "5"}),
                "minimumGenerations must be an integer >= 1",
            ),
            (
                json!({"minimumGenerations": null}),
                "minimumGenerations must be an integer >= 1",
            ),
            (
                json!({"minimumAgeDays": -1}),
                "minimumAgeDays must be an integer >= 0",
            ),
            (
                json!({"minimumAgeDays": false}),
                "minimumAgeDays must be an integer >= 0",
            ),
            (
                json!({"checkpointInterval": 0}),
                "checkpointInterval must be an integer >= 1",
            ),
            (
                json!({"keepMutationClasses": "x"}),
                "keepMutationClasses must be a list of strings",
            ),
            (
                json!({"keepMutationClasses": null}),
                "keepMutationClasses must be a list of strings",
            ),
            (
                json!({"keepMutationClasses": 3}),
                "keepMutationClasses must be a list of strings",
            ),
        ];
        for (payload, message) in cases {
            let error = normalize_retention_policy(&payload).unwrap_err();
            assert_eq!(error.message, message, "{payload}");
            assert_eq!(error.status, 400);
        }
    }

    #[test]
    fn version_equality_accepts_python_numeric_equality() {
        // `1.0 == 1` and `True == 1` in Python; `"1"` and `null` do not.
        for payload in [
            json!({"retentionPolicyVersion": 1}),
            json!({"retentionPolicyVersion": 1.0}),
            json!({"retentionPolicyVersion": true}),
        ] {
            assert!(
                normalize_retention_policy(&payload).is_ok(),
                "{payload} should be accepted"
            );
        }
    }

    #[test]
    fn keep_classes_are_stripped_sorted_and_deduplicated() {
        let normalized = normalize_retention_policy(&json!({
            "keepMutationClasses": [" b ", "a", "a", "", null, 1],
        }))
        .unwrap();
        assert_eq!(
            normalized["keepMutationClasses"],
            json!(["1", "None", "a", "b"])
        );
        // A present empty list is honoured (`data.get` found it); it is not the
        // same as omitting the key.
        let empty = normalize_retention_policy(&json!({"keepMutationClasses": []})).unwrap();
        assert_eq!(empty["keepMutationClasses"], json!([]));
        let omitted = normalize_retention_policy(&json!({})).unwrap();
        assert_eq!(
            omitted["keepMutationClasses"],
            defaults()["keepMutationClasses"]
        );
    }

    #[test]
    fn non_string_members_become_python_text() {
        let normalized = normalize_retention_policy(&json!({
            "keepMutationClasses": [true, [["x"]], {"k": "v"}],
        }))
        .unwrap();
        // `str([["x"]])` renders the single-element list as `[['x']]`, not `['x']`.
        assert_eq!(
            normalized["keepMutationClasses"],
            json!(["True", "[['x']]", "{'k': 'v'}"])
        );
    }

    #[test]
    fn booleans_are_not_integers() {
        assert_eq!(python_exact_int(&json!(true)), None);
        assert_eq!(python_exact_int(&json!(false)), None);
        assert_eq!(python_exact_int(&json!(7)), Some(7));
        assert_eq!(python_exact_int(&json!(7.0)), None);
        assert_eq!(python_exact_int(&json!("7")), None);
    }
}
