//! Failover source-target switch for remote restore sessions:
//! `POST /api/workspace/disaster-recovery/failover/{restore_id}`
//! (`backup_remote_restore.attempt_target_failover`).
//!
//! Writes `.restore-staging/{restore_id}/remote-fetch.json` (`restore_staging_store`
//! domain, `DEEPSEEK_RUNTIME_MODE=python_disabled` only; default mode refuses with
//! `409 NATIVE_RESTORE_STAGING_STORE_WRITE_NOT_OWNED` without creating files).
//! Always establishes holds on the new target before durably recording the switch
//! and releasing old holds afterwards.

use std::collections::HashSet;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::Path;

use axum::Router;
use axum::body::Bytes;
use axum::extract::Path as AxumPath;
use axum::http::{HeaderMap, Method};
use axum::response::Response;
use axum::routing::any;
use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::core_utils::python_truthy;
use deepseek_policy::python_json::value_str;
use serde_json::{Map, Value, json};

use crate::backup_catalog_routes::utc_iso_from_unix;
use crate::backup_dr_audit_routes::resolve_target_root;
use crate::backup_dr_slo_routes::now_unix_secs;
use crate::backup_restore_tx_routes::{
    read_remote_restore_session, remote_session_path, write_atomic_remote_session,
};
use crate::backup_restore_write_routes::{finish_json, require_write_authority};
use crate::route_support;

const MAX_FAILOVER_BODY_BYTES: i64 = 16_000;
const DEFAULT_MAX_FAILOVERS: usize = 3;
const HOLD_TTL_SECONDS: i64 = 6 * 3600;
const OBJECT_SET_V1: &str = "object-set-v1";

pub fn router() -> Router {
    Router::new().route(
        "/api/workspace/disaster-recovery/failover/:restore_id",
        any(dispatch_failover),
    )
}

async fn dispatch_failover(
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
    let payload = match route_support::read_json_object(&headers, &body, MAX_FAILOVER_BODY_BYTES) {
        Ok(map) => map,
        Err(err) => return route_support::app_error_response(err),
    };
    let reason = match payload.get("reason") {
        Some(v) if python_truthy(v) => value_str(v),
        _ => "network-unavailable".to_string(),
    };
    let joined = tokio::task::spawn_blocking(move || {
        let root = crate::data_routes::workspace_root();
        attempt_target_failover(&root, &restore_id, &reason)
    })
    .await;
    finish_json(joined)
}

fn failover_allowed(phase: &str) -> bool {
    matches!(
        phase,
        "created"
            | "preflight"
            | "preflighted"
            | "fetching"
            | "fetching-controls"
            | "controls-fetched"
            | "fetching-selected-components"
            | "components-fetched"
            | "fetching-chain"
            | "chain-fetched"
            | "fetched"
            | "planning-projection"
            | "preview-planned"
    )
}

fn retryable_failover_reason(reason: &str) -> bool {
    matches!(
        reason,
        "network-unavailable"
            | "remote-5xx"
            | "remote-object-missing"
            | "remote-object-corrupt"
            | "target-health-failure"
            | "hold-acquire-failed"
            | "transfer-failed"
    )
}

fn truthy_opt(value: Option<&Value>) -> Option<String> {
    match value {
        Some(v) if python_truthy(v) => Some(value_str(v)),
        _ => None,
    }
}

fn select_next_candidate(
    plan: &Map<String, Value>,
    current_target: &str,
    attempted_targets: &[String],
) -> Option<String> {
    let mut attempted: HashSet<String> = attempted_targets.iter().cloned().collect();
    if !current_target.is_empty() {
        attempted.insert(current_target.to_string());
    }
    let max_failovers = plan
        .get("maxFailovers")
        .filter(|v| python_truthy(v))
        .and_then(Value::as_i64)
        .map(|n| n.max(0) as usize)
        .unwrap_or(DEFAULT_MAX_FAILOVERS);
    if attempted.len().saturating_sub(1) >= max_failovers {
        return None;
    }
    let ordered = plan.get("orderedCandidates").and_then(Value::as_array)?;
    for cand in ordered {
        if let Some(tid) = cand.as_object().and_then(|o| truthy_opt(o.get("targetId"))) {
            if !attempted.contains(&tid) {
                return Some(tid);
            }
        }
    }
    None
}

fn put_json_if_absent(target_root: &Path, key: &str, payload: &Value) -> Result<(), AppError> {
    let rel = key.trim_start_matches('/').replace('\\', "/");
    if rel.is_empty()
        || rel
            .split('/')
            .any(|seg| seg.is_empty() || seg == "." || seg == "..")
    {
        return Err(AppError::invalid_payload("Invalid hold key"));
    }
    let mut full = target_root.to_path_buf();
    for seg in rel.split('/') {
        full.push(seg);
    }
    if let Some(parent) = full.parent() {
        fs::create_dir_all(parent).map_err(|_| route_support::unhandled_error())?;
    }
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&full)
        .map_err(|err| {
            if err.kind() == std::io::ErrorKind::AlreadyExists {
                AppError {
                    message: "conditional-put-failed: already exists".to_string(),
                    code: codes::INVALID_REQUEST,
                    status: 409,
                }
            } else {
                route_support::unhandled_error()
            }
        })?;
    let body =
        serde_json::to_string_pretty(payload).map_err(|_| route_support::unhandled_error())?;
    file.write_all(body.as_bytes())
        .and_then(|()| file.write_all(b"\n"))
        .and_then(|()| file.flush())
        .map_err(|_| route_support::unhandled_error())?;
    let _ = file.sync_all();
    Ok(())
}

fn delete_hold_best_effort(target_root: &Path, key: &str) {
    let rel = key.trim_start_matches('/').replace('\\', "/");
    if rel.is_empty()
        || rel
            .split('/')
            .any(|seg| seg.is_empty() || seg == "." || seg == "..")
    {
        return;
    }
    let mut full = target_root.to_path_buf();
    for seg in rel.split('/') {
        full.push(seg);
    }
    if full.is_file() {
        let _ = fs::remove_file(full);
    }
}

pub(crate) fn attempt_target_failover(
    root: &Path,
    restore_id: &str,
    failure_reason: &str,
) -> Result<Value, AppError> {
    let path = remote_session_path(root, restore_id)?;
    let Some(mut session) = read_remote_restore_session(root, restore_id)? else {
        return Err(AppError::not_found("Remote restore session not found"));
    };

    let phase = session
        .get("phase")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    if !failover_allowed(&phase) {
        return Err(AppError {
            message: format!("Automatic target failover forbidden in phase {phase}"),
            code: codes::INVALID_REQUEST,
            status: 409,
        });
    }
    if !retryable_failover_reason(failure_reason) {
        return Err(AppError {
            message: format!(
                "Failure reason is not retryable via target failover: {failure_reason}"
            ),
            code: codes::INVALID_REQUEST,
            status: 409,
        });
    }
    let Some(plan) = session
        .get("recoveryPlan")
        .and_then(Value::as_object)
        .cloned()
    else {
        return Err(AppError {
            message: "Recovery plan is required for automatic failover".to_string(),
            code: codes::INVALID_REQUEST,
            status: 409,
        });
    };

    let current_target = truthy_opt(session.get("activeSourceTargetId"))
        .or_else(|| truthy_opt(session.get("targetId")))
        .unwrap_or_default();
    let attempted: Vec<String> = session
        .get("attemptedSourceTargets")
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .filter(|v| !v.is_null())
                .map(value_str)
                .filter(|s| !s.is_empty())
                .collect()
        })
        .unwrap_or_default();

    let Some(new_target_id) = select_next_candidate(&plan, &current_target, &attempted) else {
        return Err(AppError {
            message: "No remaining failover candidates".to_string(),
            code: codes::INVALID_REQUEST,
            status: 409,
        });
    };

    let backup_id = truthy_opt(session.get("backupId")).unwrap_or_default();
    let mut old_hold_keys: Vec<String> = session
        .get("holdKeys")
        .and_then(Value::as_array)
        .map(|arr| arr.iter().filter(|v| !v.is_null()).map(value_str).collect())
        .unwrap_or_default();
    if old_hold_keys.is_empty() {
        if let Some(hk) = truthy_opt(session.get("holdKey")) {
            old_hold_keys.push(hk);
        }
    }

    let new_target_root = resolve_target_root(&new_target_id)?;
    let next_failover_count = session
        .get("failoverCount")
        .and_then(Value::as_i64)
        .unwrap_or(0)
        + 1;
    let now_secs = now_unix_secs();
    let now_iso = utc_iso_from_unix(now_secs);
    let expires_at = utc_iso_from_unix(now_secs + HOLD_TTL_SECONDS);

    let chain: Vec<Value> = session
        .get("chain")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let mut new_hold_keys = Vec::new();
    let mut new_holds = Vec::new();

    if !chain.is_empty() {
        for (index, member) in chain.iter().enumerate() {
            let member_obj = member.as_object();
            let member_backup_id = member_obj
                .and_then(|o| truthy_opt(o.get("backupId")))
                .unwrap_or_else(|| backup_id.clone());
            let hold = json!({
                "schemaVersion": 3,
                "storageProtocol": session.get("storageProtocol").cloned().unwrap_or(Value::Null),
                "restoreId": restore_id,
                "backupId": member_backup_id,
                "objectSetDigest": member_obj.and_then(|o| o.get("objectSetDigest")).cloned().unwrap_or(Value::Null),
                "objectDigest": member_obj.and_then(|o| o.get("objectDigest")).cloned().unwrap_or(Value::Null),
                "objects": member_obj.and_then(|o| o.get("objects")).filter(|v| v.is_array()).cloned().unwrap_or(Value::Null),
                "targetId": new_target_id,
                "createdAt": now_iso,
                "generation": 0,
                "expiresAt": expires_at,
            });
            let key = format!("holds/restore/{restore_id}-fo{next_failover_count}-{index}.json");
            put_json_if_absent(&new_target_root, &key, &hold)?;
            new_hold_keys.push(key.clone());
            let mut hold_entry = hold.as_object().cloned().unwrap_or_default();
            hold_entry.insert("holdKey".into(), Value::String(key));
            new_holds.push(Value::Object(hold_entry));
        }
    } else {
        let hold = json!({
            "schemaVersion": 3,
            "restoreId": restore_id,
            "backupId": backup_id,
            "objectDigest": session.get("objectDigest").cloned().unwrap_or(Value::Null),
            "targetId": new_target_id,
            "createdAt": now_iso,
            "generation": 0,
            "expiresAt": expires_at,
        });
        let key = format!("holds/restore/{restore_id}-fo{next_failover_count}.json");
        put_json_if_absent(&new_target_root, &key, &hold)?;
        new_hold_keys.push(key.clone());
        let mut hold_entry = hold.as_object().cloned().unwrap_or_default();
        hold_entry.insert("holdKey".into(), Value::String(key));
        new_holds.push(Value::Object(hold_entry));
    }

    let switch_event = json!({
        "fromTargetId": current_target,
        "toTargetId": new_target_id,
        "reason": failure_reason,
        "phase": phase,
        "at": now_iso,
        "failoverIndex": next_failover_count,
        "oldHoldKeys": old_hold_keys,
        "newHoldKeys": new_hold_keys,
    });

    let mut seen_attempted = HashSet::new();
    let mut updated_attempted = Vec::new();
    for tid in attempted
        .into_iter()
        .chain([current_target.clone(), new_target_id.clone()])
    {
        if !tid.is_empty() && seen_attempted.insert(tid.clone()) {
            updated_attempted.push(Value::String(tid));
        }
    }

    let mut events = session
        .get("targetSwitchEvents")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    events.push(switch_event.clone());

    let next_phase =
        if session.get("storageProtocol").and_then(Value::as_str) == Some(OBJECT_SET_V1) {
            "fetching-controls"
        } else {
            "fetching"
        };

    session.insert(
        "previousSourceTargetId".into(),
        Value::String(current_target.clone()),
    );
    session.insert(
        "activeSourceTargetId".into(),
        Value::String(new_target_id.clone()),
    );
    session.insert("targetId".into(), Value::String(new_target_id.clone()));
    session.insert(
        "holdKeys".into(),
        Value::Array(new_hold_keys.iter().cloned().map(Value::String).collect()),
    );
    session.insert("holds".into(), Value::Array(new_holds));
    session.insert(
        "holdKey".into(),
        new_hold_keys
            .first()
            .cloned()
            .map(Value::String)
            .unwrap_or(Value::Null),
    );
    session.insert(
        "attemptedSourceTargets".into(),
        Value::Array(updated_attempted),
    );
    session.insert("failoverCount".into(), json!(next_failover_count));
    session.insert("targetSwitchEvents".into(), Value::Array(events));
    session.insert("updatedAt".into(), Value::String(now_iso));
    if session.get("downloadedBytes").is_some_and(|v| !v.is_null()) {
        session.insert("downloadedBytes".into(), json!(0));
    }
    session.insert("phase".into(), Value::String(next_phase.to_string()));

    write_atomic_remote_session(&path, &mut session, false, false, false)?;

    if !current_target.is_empty() && !old_hold_keys.is_empty() {
        if let Ok(old_root) = resolve_target_root(&current_target) {
            for key in &old_hold_keys {
                delete_hold_best_effort(&old_root, key);
            }
        }
    }

    Ok(json!({
        "restoreId": restore_id,
        "phase": next_phase,
        "activeSourceTargetId": new_target_id,
        "previousSourceTargetId": current_target,
        "failoverCount": next_failover_count,
        "switchEvent": switch_event,
        "holdKeys": new_hold_keys,
        "newHoldBeforeOldRelease": true,
    }))
}
