//! Write continuity & primary promotion routes (S4):
//! - `GET /api/workspace/backup-policies/{policy_id}/continuity`
//! - `POST /api/workspace/backup-policies/{policy_id}/promote-primary`
//!
//! `.backup-continuity/<policy_id>.json` belongs to `backup_write_continuity_store`
//! (a declared native data domain). Reads never create `.backup-continuity/`.
//! `POST .../promote-primary` fails closed with
//! `409 NATIVE_BACKUP_WRITE_CONTINUITY_WRITE_NOT_OWNED` unless
//! `DEEPSEEK_RUNTIME_MODE=python_disabled`.

use std::fs;
use std::path::PathBuf;

use axum::Router;
use axum::body::Bytes;
use axum::extract::Path as AxumPath;
use axum::http::{HeaderMap, Method, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::core_utils::python_truthy;
use rusqlite::Connection;
use serde_json::{Map, Value, json};

use crate::backup_catalog_routes::{
    open_readonly_sqlite, registered_target, unix_seconds, utc_iso_from_unix,
};
use crate::backup_catalog_write_routes::verify_registered_target_ready_readonly;
use crate::backup_retention_routes::load_backup_policy;
use crate::route_support;

const BODY_LIMIT: i64 = 16_000;
const WRITE_NOT_OWNED: &str = "NATIVE_BACKUP_WRITE_CONTINUITY_WRITE_NOT_OWNED";

pub fn router() -> Router {
    Router::new()
        .route(
            "/api/workspace/backup-policies/:policy_id/continuity",
            any(dispatch_continuity),
        )
        .route(
            "/api/workspace/backup-policies/:policy_id/promote-primary",
            any(dispatch_promote_primary),
        )
}

fn continuity_dir() -> PathBuf {
    crate::data_routes::workspace_root().join(".backup-continuity")
}

fn valid_id(id: &str) -> bool {
    !id.is_empty()
        && id != "."
        && id != ".."
        && !id.contains('/')
        && !id.contains('\\')
        && !id.contains('\0')
}

async fn dispatch_continuity(method: Method, AxumPath(policy_id): AxumPath<String>) -> Response {
    if method == Method::OPTIONS {
        return route_support::options_response();
    }
    if method != Method::GET && method != Method::HEAD {
        return route_support::method_not_allowed();
    }
    let is_head = method == Method::HEAD;
    match tokio::task::spawn_blocking(move || read_continuity_state(&policy_id)).await {
        Ok(Ok(_)) if is_head => StatusCode::OK.into_response(),
        Ok(Ok(state)) => axum::Json(state).into_response(),
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
    }
}

async fn dispatch_promote_primary(
    method: Method,
    AxumPath(policy_id): AxumPath<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if method == Method::OPTIONS {
        return route_support::options_response();
    }
    if method != Method::POST {
        return route_support::method_not_allowed();
    }
    match tokio::task::spawn_blocking(move || handle_promote_primary(&policy_id, &headers, &body))
        .await
    {
        Ok(Ok(res)) => axum::Json(res).into_response(),
        Ok(Err(error)) => route_support::app_error_response(error),
        Err(_) => route_support::app_error_response(route_support::unhandled_error()),
    }
}

fn default_continuity_state(policy_id: &str, primary_id: &str, rev: i64, now: &str) -> Value {
    json!({
        "schemaVersion": 1,
        "policyId": policy_id,
        "policyRevision": rev,
        "failoverEpoch": 0,
        "configuredPrimaryTargetId": primary_id,
        "activeWriteTargetId": primary_id,
        "activeWriteTargetRole": "primary",
        "lastFailoverAt": Value::Null,
        "lastFailoverReason": Value::Null,
        "lastFailbackAt": Value::Null,
        "lastFailbackReason": Value::Null,
        "primaryFirstHealthyAt": now,
        "primaryLastHealthyAt": now,
        "primaryConsecutiveHealthySeconds": 0.0,
        "failoverActiveSince": Value::Null,
        "targetLiveness": {},
        "updatedAt": now,
    })
}

fn policy_primary_and_revision(policy_id: &str) -> (String, i64) {
    let Ok(policy) = load_backup_policy(policy_id) else {
        return ("managed-local".to_string(), 1);
    };
    let primary = policy
        .get("primaryTargetId")
        .or_else(|| policy.get("targetId"))
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .unwrap_or("managed-local")
        .to_string();
    let rev = policy
        .get("policyRevision")
        .and_then(Value::as_i64)
        .filter(|value| *value > 0)
        .unwrap_or(1);
    (primary, rev)
}

pub(crate) fn read_continuity_state(policy_id: &str) -> Result<Value, AppError> {
    if !valid_id(policy_id) {
        return Err(AppError::invalid_payload("Invalid policy id"));
    }
    let path = continuity_dir().join(format!("{policy_id}.json"));
    if path.is_file() {
        if let Ok(text) = fs::read_to_string(&path) {
            if let Ok(Value::Object(obj)) = serde_json::from_str::<Value>(&text) {
                return Ok(Value::Object(obj));
            }
        }
        let now = utc_iso_from_unix(unix_seconds());
        return Ok(json!({
            "schemaVersion": 1,
            "policyId": policy_id,
            "policyRevision": 1,
            "failoverEpoch": 0,
            "configuredPrimaryTargetId": "managed-local",
            "activeWriteTargetId": "managed-local",
            "activeWriteTargetRole": "primary",
            "updatedAt": now,
        }));
    }
    let (primary, rev) = policy_primary_and_revision(policy_id);
    let now = utc_iso_from_unix(unix_seconds());
    Ok(default_continuity_state(policy_id, &primary, rev, &now))
}

fn parse_optional_int(payload: &Map<String, Value>, field: &str) -> Result<Option<i64>, AppError> {
    let Some(value) = payload.get(field) else {
        return Ok(None);
    };
    match value {
        Value::Null => Ok(None),
        Value::Number(num) => num
            .as_i64()
            .map(Some)
            .ok_or_else(|| AppError::invalid_payload(format!("{field} must be an integer"))),
        Value::String(text) => text
            .trim()
            .parse::<i64>()
            .map(Some)
            .map_err(|_| AppError::invalid_payload(format!("{field} must be an integer"))),
        _ => Err(AppError::invalid_payload(format!(
            "{field} must be an integer"
        ))),
    }
}

fn parse_optional_string(payload: &Map<String, Value>, field: &str) -> Option<String> {
    match payload.get(field) {
        Some(Value::Null) | None => None,
        Some(other) => Some(deepseek_policy::python_json::value_str(other)),
    }
}

fn invalid_request(status: u16, message: impl Into<String>) -> AppError {
    AppError {
        status,
        code: codes::INVALID_REQUEST,
        message: message.into(),
    }
}

fn verify_latest_recovery_point(
    policy_id: &str,
    target_id: &str,
    expected_backup_id: Option<&str>,
    expected_object_set_digest: Option<&str>,
) -> Result<(), AppError> {
    let dr_db = crate::data_routes::workspace_root()
        .join(".backup-dr")
        .join("evidence.sqlite3");
    let Some(conn) = open_readonly_sqlite(&dr_db)? else {
        return Ok(());
    };
    let has_table: bool = conn
        .query_row(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'recovery_point_copies'",
            [],
            |_| Ok(true),
        )
        .unwrap_or(false);
    if !has_table {
        return Ok(());
    }
    let rows = load_recoverable_copies(&conn, policy_id)?;
    if rows.is_empty() {
        return Ok(());
    }
    let global_latest = rows
        .iter()
        .max_by(|a, b| a.committed_at.cmp(&b.committed_at))
        .expect("non-empty rows");
    if let Some(expected_bid) = expected_backup_id {
        if global_latest.backup_id != expected_bid {
            return Err(invalid_request(
                412,
                format!(
                    "CAS mismatch on expected_latest_backup_id: expected {expected_bid}, global actual {}",
                    global_latest.backup_id
                ),
            ));
        }
    }
    if let Some(expected_osd) = expected_object_set_digest {
        if !global_latest.object_set_digest.is_empty()
            && global_latest.object_set_digest != expected_osd
        {
            return Err(invalid_request(
                412,
                format!(
                    "CAS mismatch on expected_latest_object_set_digest: expected {expected_osd}, global actual {}",
                    global_latest.object_set_digest
                ),
            ));
        }
    }
    let target_has_latest = rows
        .iter()
        .any(|copy| copy.target_id == target_id && copy.backup_id == global_latest.backup_id);
    if !target_has_latest {
        return Err(invalid_request(
            409,
            format!(
                "primary-promotion-rejected: Target {target_id} does not possess global latest logical recovery point {}",
                global_latest.backup_id
            ),
        ));
    }
    Ok(())
}

struct RecoverableCopy {
    target_id: String,
    backup_id: String,
    object_set_digest: String,
    committed_at: String,
}

fn load_recoverable_copies(
    conn: &Connection,
    policy_id: &str,
) -> Result<Vec<RecoverableCopy>, AppError> {
    let mut stmt = conn
        .prepare(
            "SELECT target_id, backup_id, COALESCE(object_set_digest, ''), committed_at \
             FROM recovery_point_copies \
             WHERE policy_id = ?1 AND recoverable = 1 AND state = 'healthy' \
             ORDER BY committed_at DESC, logical_id DESC LIMIT 200",
        )
        .map_err(|_| route_support::unhandled_error())?;
    let mut rows = stmt
        .query([policy_id])
        .map_err(|_| route_support::unhandled_error())?;
    let mut out = Vec::new();
    while let Some(row) = rows.next().map_err(|_| route_support::unhandled_error())? {
        out.push(RecoverableCopy {
            target_id: row.get(0).map_err(|_| route_support::unhandled_error())?,
            backup_id: row.get(1).map_err(|_| route_support::unhandled_error())?,
            object_set_digest: row.get(2).map_err(|_| route_support::unhandled_error())?,
            committed_at: row.get(3).map_err(|_| route_support::unhandled_error())?,
        });
    }
    Ok(out)
}

fn check_no_active_repairs(policy_id: &str, target_id: &str) -> Result<(), AppError> {
    let repairs_dir = crate::data_routes::workspace_root()
        .join(".backup-replication")
        .join("repairs");
    if !repairs_dir.is_dir() {
        return Ok(());
    }
    let entries = fs::read_dir(&repairs_dir).map_err(|_| route_support::unhandled_error())?;
    for entry in entries.flatten() {
        let name = entry.file_name();
        let Some(name_str) = name.to_str() else {
            continue;
        };
        if name_str.starts_with('.') || !name_str.ends_with(".json") || !entry.path().is_file() {
            continue;
        }
        let Ok(text) = fs::read_to_string(entry.path()) else {
            continue;
        };
        let Ok(Value::Object(job)) = serde_json::from_str::<Value>(&text) else {
            continue;
        };
        let j_pol = job.get("policyId").and_then(Value::as_str).unwrap_or("");
        let j_dest = job
            .get("destTargetId")
            .and_then(Value::as_str)
            .unwrap_or("");
        let j_phase = job.get("phase").and_then(Value::as_str).unwrap_or("");
        if j_pol == policy_id
            && j_dest == target_id
            && !matches!(
                j_phase,
                "complete" | "healthy" | "failed" | "failed-terminal"
            )
        {
            return Err(invalid_request(
                409,
                format!("Target {target_id} has active repair jobs for policy {policy_id}"),
            ));
        }
    }
    Ok(())
}

fn verify_target_promotable(target_id: &str) -> Result<(), AppError> {
    if target_id == "managed-local" {
        return Ok(());
    }
    let target = registered_target(target_id)?;
    let drain_state = target
        .get("drainState")
        .and_then(Value::as_str)
        .unwrap_or("active");
    if matches!(drain_state, "draining" | "drained") {
        return Err(invalid_request(
            400,
            format!("Target {target_id} is in {drain_state} state"),
        ));
    }
    let kind = target
        .get("kind")
        .and_then(Value::as_str)
        .unwrap_or("filesystem");
    if kind == "s3" {
        let last_ready = target
            .get("lastProbe")
            .and_then(Value::as_object)
            .and_then(|probe| probe.get("scheduledBackupReady"))
            .is_some_and(python_truthy);
        if !last_ready {
            return Err(AppError {
                status: 503,
                code: "NATIVE_BACKUP_CATALOG_TARGET_STORE_UNMIGRATED",
                message: format!(
                    "Target {target_id} S3 readiness probe is unavailable in the native runtime"
                ),
            });
        }
        return Ok(());
    }
    let path_str = target.get("path").and_then(Value::as_str).unwrap_or("");
    let target_root = PathBuf::from(path_str);
    verify_registered_target_ready_readonly(target_id, &target_root, &target).map_err(
        |detail| {
            invalid_request(
                400,
                format!("Target {target_id} liveness preflight failed: {detail}"),
            )
        },
    )?;
    Ok(())
}

fn handle_promote_primary(
    policy_id: &str,
    headers: &HeaderMap,
    body: &[u8],
) -> Result<Value, AppError> {
    if !valid_id(policy_id) {
        return Err(AppError::invalid_payload("Invalid policy id"));
    }
    let payload = route_support::read_json_object(headers, body, BODY_LIMIT)?;
    let target_id = payload
        .get("targetId")
        .map(deepseek_policy::python_json::value_str)
        .unwrap_or_default()
        .trim()
        .to_string();
    if target_id.is_empty() {
        return Err(AppError::invalid_payload(
            "targetId is required for primary promotion",
        ));
    }
    let exp_rev = parse_optional_int(&payload, "expectedPolicyRevision")?;
    let exp_epoch = parse_optional_int(&payload, "expectedFailoverEpoch")?;
    let exp_bid = parse_optional_string(&payload, "expectedLatestBackupId");
    let exp_osd = parse_optional_string(&payload, "expectedLatestObjectSetDigest");

    route_support::require_native_store("backup_write_continuity_store", WRITE_NOT_OWNED)?;

    let policy = load_backup_policy(policy_id)?;
    let mut state = read_continuity_state(policy_id)?;
    let policy_rev = policy
        .get("policyRevision")
        .and_then(Value::as_i64)
        .unwrap_or(1);
    let state_rev = state
        .get("policyRevision")
        .and_then(Value::as_i64)
        .unwrap_or(policy_rev);
    let curr_rev = policy_rev.max(state_rev);
    let curr_epoch = state
        .get("failoverEpoch")
        .and_then(Value::as_i64)
        .unwrap_or(0);

    if let Some(expected) = exp_rev {
        if curr_rev != expected {
            return Err(invalid_request(
                412,
                format!("CAS mismatch on policyRevision: expected {expected}, actual {curr_rev}"),
            ));
        }
    }
    if let Some(expected) = exp_epoch {
        if curr_epoch != expected {
            return Err(invalid_request(
                412,
                format!("CAS mismatch on failoverEpoch: expected {expected}, actual {curr_epoch}"),
            ));
        }
    }

    let prev_primary = policy
        .get("primaryTargetId")
        .or_else(|| policy.get("targetId"))
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .unwrap_or("managed-local")
        .to_string();
    let repl = policy.get("replication").and_then(Value::as_object);
    if let Some(repl_obj) = repl {
        if repl_obj.get("enabled").is_some_and(python_truthy) {
            let in_replicas = repl_obj
                .get("targets")
                .and_then(Value::as_array)
                .is_some_and(|entries| {
                    entries.iter().any(|entry| {
                        entry
                            .as_object()
                            .and_then(|obj| obj.get("targetId"))
                            .and_then(Value::as_str)
                            == Some(target_id.as_str())
                    })
                });
            if !in_replicas && target_id != prev_primary {
                return Err(invalid_request(
                    400,
                    format!("Target {target_id} is not a configured replica in policy {policy_id}"),
                ));
            }
        }
    }

    verify_target_promotable(&target_id)?;
    verify_latest_recovery_point(
        policy_id,
        &target_id,
        exp_bid.as_deref(),
        exp_osd.as_deref(),
    )?;
    check_no_active_repairs(policy_id, &target_id)?;

    let now_str = utc_iso_from_unix(unix_seconds());
    let next_rev = curr_rev + 1;
    let next_epoch = curr_epoch + 1;
    let Some(state_obj) = state.as_object_mut() else {
        return Err(route_support::unhandled_error());
    };
    state_obj.insert("schemaVersion".to_string(), json!(1));
    state_obj.insert("policyId".to_string(), json!(policy_id));
    state_obj.insert("policyRevision".to_string(), json!(next_rev));
    state_obj.insert("failoverEpoch".to_string(), json!(next_epoch));
    state_obj.insert("configuredPrimaryTargetId".to_string(), json!(target_id));
    state_obj.insert("activeWriteTargetId".to_string(), json!(target_id));
    state_obj.insert("activeWriteTargetRole".to_string(), json!("primary"));
    state_obj.insert("failoverActiveSince".to_string(), Value::Null);
    state_obj.insert("lastFailoverAt".to_string(), Value::Null);
    state_obj.insert("lastFailbackAt".to_string(), json!(now_str));
    state_obj.insert(
        "lastFailbackReason".to_string(),
        json!(format!("promoted-target-{target_id}-as-primary")),
    );
    state_obj.insert("primaryFirstHealthyAt".to_string(), json!(now_str));
    state_obj.insert("primaryLastHealthyAt".to_string(), json!(now_str));
    state_obj.insert("primaryConsecutiveHealthySeconds".to_string(), json!(0.0));
    state_obj.insert("updatedAt".to_string(), json!(now_str));

    route_support::write_json_atomic(
        &continuity_dir().join(format!("{policy_id}.json")),
        &Value::Object(state_obj.clone()),
    )?;

    Ok(json!({
        "status": "promoted",
        "policyId": policy_id,
        "policyRevision": next_rev,
        "failoverEpoch": next_epoch,
        "previousPrimaryTargetId": prev_primary,
        "newPrimaryTargetId": target_id,
        "promotedPrimaryTargetId": target_id,
        "promotedAt": now_str,
    }))
}
