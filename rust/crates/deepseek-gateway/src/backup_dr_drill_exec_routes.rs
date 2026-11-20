//! Native S6 isolated recovery drill execution routes:
//! - `POST /api/workspace/disaster-recovery/drills` (`run_recovery_drill`)
//! - `POST /api/workspace/disaster-recovery/drills/schedule/{policy_id}` (`execute_scheduled_drill`)
//!
//! Enforces `restore_staging_store` (`DEEPSEEK_RUNTIME_MODE=python_disabled` only;
//! default mode refuses with `409 NATIVE_RESTORE_STAGING_STORE_WRITE_NOT_OWNED`
//! without creating directories or SQLite files).

use std::collections::HashSet;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::Instant;

use axum::Router;
use axum::body::Bytes;
use axum::extract::Path as AxumPath;
use axum::http::{HeaderMap, Method};
use axum::response::Response;
use axum::routing::any;
use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::backup_projection::OBJECT_SET_V1;
use deepseek_policy::backup_unlock::scrub_plaintext_file;
use deepseek_policy::core_utils::python_truthy;
use serde_json::{Map, Value, json};

use crate::backup_archive_routes::{
    InspectMeta, inspect_projected_restore_tree, inspect_verified_tree, tree_digest,
};
use crate::backup_capacity_routes::now_utc_iso;
use crate::backup_catalog_routes::open_readonly_sqlite;
use crate::backup_dr_audit_routes::{
    DrillEvidenceRecord, StageSampleRecord, open_dr_ledger_write, record_drill_evidence,
    record_stage_sample,
};
use crate::backup_dr_status_routes::{
    latest_drill_outcome, latest_recoverable_point, list_logical_recovery_copies,
};
use crate::backup_remote_restore_routes::{
    PreflightFailure, create_restore_from_target, fetch_restore_session,
    materialize_restore_session, preflight_restore_session, restore_members,
};
use crate::backup_restore_read_routes::{read_json_object, validate_restore_id};
use crate::backup_restore_tx_routes::{
    read_remote_restore_session, release_session_holds, remote_session_path,
    write_atomic_remote_session,
};
use crate::backup_restore_write_routes::{finish_json, require_write_authority, write_atomic_json};
use crate::backup_secret_slots::{clear_secret, consume_secret, has_secret, put_secret_bytes};
use crate::resilience_advisory_routes::list_policies_readonly;
use crate::route_support;

const MAX_BODY_BYTES: i64 = 16_000;
const SCHEMA_VERSION: i64 = 1;
const RESULT_NAME: &str = "drill-result.json";
const CLAIM_NAME: &str = "drill-running.json";

const DISALLOWED_PHASES: [&str; 9] = [
    "preparing",
    "prepared",
    "committing",
    "complete",
    "aborting",
    "aborted",
    "rolled-back",
    "failed",
    "recovery-required",
];

const PLAINTEXT_FILES: [&str; 2] = ["plan.json", "verified-plan.json"];
const PLAINTEXT_DIR_NAMES: [&str; 4] = ["extracted", "verified", "staged", "rollback"];
const PLAINTEXT_DIR_PREFIXES: [&str; 4] =
    ["metadata-", "object-layer-", "extracted-", "projected-"];

pub fn router() -> Router {
    Router::new()
        .route(
            "/api/workspace/disaster-recovery/drills",
            any(dispatch_drill_create),
        )
        .route(
            "/api/workspace/disaster-recovery/drills/schedule/:policy_id",
            any(dispatch_drill_schedule),
        )
}

async fn dispatch_drill_create(method: Method, headers: HeaderMap, body: Bytes) -> Response {
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
    if payload.len() != 1 || !payload.contains_key("restoreId") {
        return route_support::app_error_response(AppError::invalid_payload(
            "Recovery Drill accepts only restoreId",
        ));
    }
    let restore_id = payload
        .get("restoreId")
        .filter(|v| python_truthy(v))
        .map(deepseek_policy::python_json::value_str)
        .unwrap_or_default();
    let joined = tokio::task::spawn_blocking(move || {
        let root = crate::data_routes::workspace_root();
        run_recovery_drill(&root, &restore_id)
    })
    .await;
    finish_json(joined)
}

async fn dispatch_drill_schedule(
    method: Method,
    AxumPath(policy_id): AxumPath<String>,
) -> Response {
    if method != Method::POST {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    if let Err(err) = require_write_authority() {
        return route_support::app_error_response(err);
    }
    let joined = tokio::task::spawn_blocking(move || {
        let root = crate::data_routes::workspace_root();
        execute_scheduled_drill(&root, &policy_id)
    })
    .await;
    finish_json(joined)
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
        Some(v) if python_truthy(v) => deepseek_policy::python_json::value_str(v),
        _ => String::new(),
    }
}

fn drill_root(root: &Path, restore_id: &str) -> Result<PathBuf, AppError> {
    validate_restore_id(restore_id)?;
    let dir = root.join(".restore-staging").join(restore_id);
    if !dir.is_dir() {
        return Err(AppError::not_found("Remote restore session not found"));
    }
    Ok(dir)
}

fn scrub_directory(path: &Path) -> Result<(), AppError> {
    if !path.is_dir() {
        return Ok(());
    }
    let mut files = Vec::new();
    let mut stack = vec![path.to_path_buf()];
    while let Some(cur) = stack.pop() {
        let Ok(entries) = fs::read_dir(&cur) else {
            continue;
        };
        for entry in entries.flatten() {
            let p = entry.path();
            if p.is_dir() {
                stack.push(p);
            } else if p.is_file() {
                files.push(p);
            }
        }
    }
    for f in files {
        scrub_plaintext_file(&f);
    }
    fs::remove_dir_all(path).map_err(|_| route_support::unhandled_error())
}

fn is_plaintext_temp_file(name: &str) -> bool {
    PLAINTEXT_FILES.contains(&name)
        || (name.starts_with("control-decrypted-") && name.ends_with(".zip"))
        || (name.starts_with("payload-decrypted-") && name.ends_with(".zip"))
        || (name.starts_with("decrypted-") && name.ends_with(".dsibackup"))
        || (name.starts_with("preview-decrypted-") && name.ends_with(".dsibackup"))
        || name == "unlocked.dsibackup"
}

fn is_plaintext_dir(name: &str) -> bool {
    PLAINTEXT_DIR_NAMES.contains(&name)
        || PLAINTEXT_DIR_PREFIXES
            .iter()
            .any(|pref| name.starts_with(pref))
}

fn scrub_plaintext(dir: &Path) -> Result<(), AppError> {
    let entries = fs::read_dir(dir).map_err(|_| route_support::unhandled_error())?;
    for entry in entries.flatten() {
        let p = entry.path();
        let name = entry.file_name().to_string_lossy().into_owned();
        if p.is_file() && is_plaintext_temp_file(&name) {
            scrub_plaintext_file(&p);
        } else if p.is_dir() && is_plaintext_dir(&name) {
            scrub_directory(&p)?;
        }
    }
    Ok(())
}

fn plaintext_remains(dir: &Path) -> bool {
    let Ok(entries) = fs::read_dir(dir) else {
        return false;
    };
    for entry in entries.flatten() {
        let p = entry.path();
        let name = entry.file_name().to_string_lossy().into_owned();
        if (p.is_file() && is_plaintext_temp_file(&name)) || (p.is_dir() && is_plaintext_dir(&name))
        {
            return true;
        }
    }
    false
}

fn compute_work(
    session: &Map<String, Value>,
    materialized: Option<&Value>,
    inspected: Option<&Value>,
) -> Value {
    let members = restore_members(session).unwrap_or_default();
    let mut components = 0usize;
    let mut ciphertext_bytes = 0_u64;
    for m in &members {
        if let Some(ctrl) = m.get("control").and_then(Value::as_object) {
            components += 1;
            ciphertext_bytes = ciphertext_bytes.saturating_add(
                ctrl.get("expectedBytes")
                    .and_then(Value::as_u64)
                    .unwrap_or(0),
            );
        } else if let Some(exp) = m.get("expectedBytes").and_then(Value::as_u64) {
            components += 1;
            ciphertext_bytes = ciphertext_bytes.saturating_add(exp);
        }
        if let Some(reqs) = m.get("requiredComponents").and_then(Value::as_array) {
            for comp in reqs {
                if comp.is_object() {
                    components += 1;
                    ciphertext_bytes = ciphertext_bytes.saturating_add(
                        comp.get("expectedBytes")
                            .and_then(Value::as_u64)
                            .unwrap_or(0),
                    );
                }
            }
        }
    }
    let manifest = materialized.and_then(|v| v.get("manifest"));
    let logical_bytes: u64 = manifest
        .and_then(|m| m.get("files"))
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .filter_map(|i| i.get("size").and_then(Value::as_u64))
                .sum()
        })
        .unwrap_or(0);
    let mut verified_contributors = inspected
        .and_then(|i| i.get("operations"))
        .and_then(Value::as_array)
        .map(|arr| arr.iter().filter(|i| i.is_object()).count())
        .unwrap_or(0);
    if verified_contributors == 0 {
        verified_contributors = manifest
            .and_then(|m| m.get("contributors"))
            .and_then(Value::as_array)
            .map(|contribs| contribs.iter().filter(|i| i.is_object()).count())
            .unwrap_or(0);
    }
    json!({
        "chainLength": members.len(),
        "components": components,
        "ciphertextBytes": ciphertext_bytes,
        "logicalBytes": logical_bytes,
        "verifiedContributors": verified_contributors,
    })
}

fn claim_drill(root: &Path, dir: &Path, restore_id: &str) -> Result<Option<Value>, AppError> {
    let result_path = dir.join(RESULT_NAME);
    if result_path.is_file() {
        return Ok(Some(Value::Object(read_json_object(&result_path)?)));
    }
    let session = read_remote_restore_session(root, restore_id)?
        .ok_or_else(|| AppError::not_found("Remote restore session not found"))?;
    let phase = truthy_str(session.get("phase"));
    if dir.join("transaction.json").is_file() || DISALLOWED_PHASES.contains(&phase.as_str()) {
        return Err(conflict_err(
            "Recovery Drill requires a pre-commit Recovery Job",
        ));
    }
    if !has_secret(restore_id) {
        return Err(conflict_err(
            "Recovery Drill requires an available unlock secret",
        ));
    }
    let claim_path = dir.join(CLAIM_NAME);
    let payload = json!({
        "schemaVersion": SCHEMA_VERSION,
        "restoreId": restore_id,
        "result": "running",
        "startedAt": now_utc_iso(),
    });
    let text =
        serde_json::to_string_pretty(&payload).map_err(|_| route_support::unhandled_error())?;
    match OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&claim_path)
    {
        Ok(mut handle) => {
            let _ = handle.write_all(text.as_bytes());
            let _ = handle.write_all(b"\n");
            let _ = handle.flush();
            Ok(None)
        }
        Err(err) if err.kind() == std::io::ErrorKind::AlreadyExists => {
            if result_path.is_file() {
                Ok(Some(Value::Object(read_json_object(&result_path)?)))
            } else {
                Err(conflict_err("Recovery Drill is already running"))
            }
        }
        Err(_) => Err(route_support::unhandled_error()),
    }
}

pub(crate) fn run_recovery_drill(root: &Path, restore_id: &str) -> Result<Value, AppError> {
    let dir = drill_root(root, restore_id)?;
    if let Some(existing) = claim_drill(root, &dir, restore_id)? {
        return Ok(existing);
    }

    let started = Instant::now();
    let started_at = now_utc_iso();
    let session = read_remote_restore_session(root, restore_id)?
        .ok_or_else(|| AppError::not_found("Remote restore session not found"))?;
    let known_work = compute_work(&session, None, None);

    let mut materialized_out: Option<Value> = None;
    let mut inspected_out: Option<Value> = None;

    let step_res: Result<Value, AppError> = (|| {
        if session.get("storageProtocol").and_then(Value::as_str) == Some(OBJECT_SET_V1) {
            if truthy_str(session.get("phase")) == "fetching-controls" {
                let ctrl = fetch_restore_session(root, restore_id, None)?;
                if truthy_str(ctrl.get("phase")) != "controls-fetched" {
                    return Err(conflict_err(
                        "Recovery Drill control fetch did not reach a verified terminal phase",
                    ));
                }
            }
            match preflight_restore_session(root, restore_id) {
                Ok(_) => {}
                Err(PreflightFailure::Error(err)) => return Err(err),
                Err(PreflightFailure::Blocked { .. }) => {
                    return Err(conflict_err("Recovery preflight is blocked"));
                }
            }
        }
        let fetched = fetch_restore_session(root, restore_id, None)?;
        let fphase = truthy_str(fetched.get("phase"));
        if !matches!(
            fphase.as_str(),
            "fetched" | "chain-fetched" | "components-fetched"
        ) {
            return Err(conflict_err(
                "Recovery Drill fetch did not reach a verified terminal phase",
            ));
        }

        let (kind, secret) = consume_secret(restore_id, None)?;
        let secret_str = secret.as_str()?.to_string();
        let materialized = materialize_restore_session(root, restore_id, &kind, &secret_str, true)?;
        let latest_session =
            read_remote_restore_session(root, restore_id)?.unwrap_or_else(|| session.clone());
        let extracted = PathBuf::from(truthy_str(materialized.get("tree")));
        let expected_dir = dir.join("extracted");
        let canon_ext = fs::canonicalize(&extracted)
            .map_err(|_| conflict_err("Recovery Drill materialized outside its isolated root"))?;
        let canon_exp = fs::canonicalize(&expected_dir)
            .map_err(|_| conflict_err("Recovery Drill materialized outside its isolated root"))?;
        if canon_ext != canon_exp {
            return Err(conflict_err(
                "Recovery Drill materialized outside its isolated root",
            ));
        }

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
            ciphertext_sha256: None,
            protection,
        };
        let inspected =
            if let Some(proj_rep) = materialized.get("projection").filter(|v| v.is_object()) {
                inspect_projected_restore_tree(
                    root,
                    manifest.clone(),
                    &extracted,
                    &dir,
                    proj_rep,
                    meta,
                )?
            } else {
                inspect_verified_tree(root, manifest.clone(), &extracted, &dir, meta)?
            };
        if inspected.get("compatible").and_then(Value::as_bool) == Some(false) {
            return Err(conflict_err(
                "Recovery Drill contributor validation is incompatible",
            ));
        }

        let ws_digest = format!("sha256:{}", tree_digest(&extracted)?);
        let source_revision = manifest
            .get("source")
            .and_then(|s| s.get("revision"))
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .unwrap_or("unknown")
            .to_string();
        let work = compute_work(&latest_session, Some(&materialized), Some(&inspected));
        materialized_out = Some(materialized);
        inspected_out = Some(inspected);

        let mut ok_map = work.as_object().cloned().unwrap_or_default();
        ok_map.insert("schemaVersion".into(), json!(SCHEMA_VERSION));
        ok_map.insert("restoreId".into(), Value::String(restore_id.into()));
        ok_map.insert("result".into(), Value::String("success".into()));
        ok_map.insert("startedAt".into(), Value::String(started_at.clone()));
        ok_map.insert("completedAt".into(), Value::String(now_utc_iso()));
        ok_map.insert(
            "durationMs".into(),
            json!(started.elapsed().as_millis() as u64),
        );
        ok_map.insert("workspaceDigest".into(), Value::String(ws_digest));
        ok_map.insert("sourceRevision".into(), Value::String(source_revision));
        Ok(Value::Object(ok_map))
    })();

    clear_secret(restore_id);
    let mut cleanup_failed = scrub_plaintext(&dir).is_err() || plaintext_remains(&dir);
    let mut current = read_remote_restore_session(root, restore_id)?.unwrap_or(session);
    release_session_holds(root, &current);
    let sp = remote_session_path(root, restore_id)?;
    let latest_work = compute_work(&current, materialized_out.as_ref(), inspected_out.as_ref());

    let mut result_map = match step_res {
        Ok(Value::Object(m)) => m,
        _ => {
            let mut m = known_work.as_object().cloned().unwrap_or_default();
            if let Some(lw) = latest_work.as_object() {
                for (k, v) in lw {
                    m.insert(k.clone(), v.clone());
                }
            }
            m.insert("schemaVersion".into(), json!(SCHEMA_VERSION));
            m.insert("restoreId".into(), Value::String(restore_id.into()));
            m.insert("result".into(), Value::String("failed".into()));
            m.insert(
                "failureCode".into(),
                Value::String("drill-validation-failed".into()),
            );
            m.insert("startedAt".into(), Value::String(started_at.clone()));
            m.insert("completedAt".into(), Value::String(now_utc_iso()));
            m.insert(
                "durationMs".into(),
                json!(started.elapsed().as_millis() as u64),
            );
            m
        }
    };

    if plaintext_remains(&dir) {
        cleanup_failed = true;
    }
    let failed_step = result_map.get("result").and_then(Value::as_str) != Some("success");
    if cleanup_failed {
        let mut cm = latest_work.as_object().cloned().unwrap_or_default();
        cm.insert("schemaVersion".into(), json!(SCHEMA_VERSION));
        cm.insert("restoreId".into(), Value::String(restore_id.into()));
        cm.insert("result".into(), Value::String("failed".into()));
        cm.insert(
            "failureCode".into(),
            Value::String("drill-cleanup-failed".into()),
        );
        cm.insert("startedAt".into(), Value::String(started_at));
        cm.insert("completedAt".into(), Value::String(now_utc_iso()));
        cm.insert(
            "durationMs".into(),
            json!(started.elapsed().as_millis() as u64),
        );
        cm.insert("cleanupCompleted".into(), json!(false));
        result_map = cm;
    } else {
        result_map.insert("cleanupCompleted".into(), json!(true));
    }

    let result_val = Value::Object(result_map.clone());
    write_atomic_json(&dir.join(RESULT_NAME), &result_val)?;
    let drill_res_str = truthy_str(result_map.get("result"));
    let completed_at = truthy_str(result_map.get("completedAt"));
    current.insert("drillOnly".into(), json!(true));
    current.insert("drillResult".into(), Value::String(drill_res_str.clone()));
    current.insert(
        "phase".into(),
        Value::String(
            if drill_res_str == "success" {
                "complete"
            } else {
                "failed"
            }
            .into(),
        ),
    );
    current.insert("updatedAt".into(), Value::String(completed_at.clone()));
    write_atomic_remote_session(&sp, &mut current, false, false, false)?;
    let _ = fs::remove_file(dir.join(CLAIM_NAME));

    let target_id = {
        let t = truthy_str(current.get("targetId"));
        if t.is_empty() {
            "managed-local".into()
        } else {
            t
        }
    };
    let backup_id = truthy_str(current.get("backupId"));
    let policy_id = truthy_str(current.get("policyId"));
    let drill_kind = if current.get("automatedDrill").is_some_and(python_truthy) {
        "automated"
    } else {
        "manual"
    };
    let duration_ms = result_map
        .get("durationMs")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let mut details = result_map.clone();
    details.insert("workClass".into(), Value::String("recovery-drill".into()));
    let _ = record_drill_evidence(&DrillEvidenceRecord {
        drill_id: restore_id,
        target_id: &target_id,
        policy_id: &policy_id,
        backup_id: &backup_id,
        drill_kind,
        observed_at: &completed_at,
        result: &drill_res_str,
        duration_ms,
        details: Some(&details),
    });

    if let Some(samples) = current
        .get("recoveryTelemetry")
        .and_then(|t| t.get("samples"))
        .and_then(Value::as_array)
    {
        for (i, sample) in samples.iter().enumerate() {
            if truthy_str(sample.get("result")) != "success" {
                continue;
            }
            let st = truthy_str(sample.get("stage"));
            let stage = if st == "materialize" {
                "materialization"
            } else {
                st.as_str()
            };
            if !matches!(stage, "transfer" | "crypto" | "materialization") {
                continue;
            }
            let sid = format!("{restore_id}-stage-{i}");
            let _ = record_stage_sample(&StageSampleRecord {
                sample_id: &sid,
                target_id: Some(&target_id),
                recovery_class: &json!({"tag": "recovery-drill"}),
                stage,
                bytes: sample.get("bytes").and_then(Value::as_i64).unwrap_or(0),
                duration_ms: sample
                    .get("durationMs")
                    .and_then(Value::as_i64)
                    .unwrap_or(0),
                observed_at: &truthy_str(sample.get("observedAt")),
                result: "success",
            });
        }
    }

    if cleanup_failed {
        return Err(AppError {
            message: "Recovery Drill cleanup failed".to_string(),
            code: codes::INTERNAL,
            status: 500,
        });
    }
    if failed_step {
        return Err(conflict_err("Recovery Drill failed"));
    }
    Ok(result_val)
}

fn select_drill_candidate_copy(
    policy: &Value,
    policy_id: &str,
    fallback_target_id: &str,
) -> Option<(String, String)> {
    let mut candidates = vec![fallback_target_id.to_string()];
    if let Some(rep) = policy.get("replication").and_then(Value::as_object) {
        if rep.get("enabled").is_some_and(python_truthy) {
            if let Some(targets) = rep.get("targets").and_then(Value::as_array) {
                for entry in targets {
                    let tid = truthy_str(entry.get("targetId"));
                    if !tid.is_empty() {
                        candidates.push(tid);
                    }
                }
            }
        }
    }
    let mut seen = HashSet::new();
    let unique: Vec<String> = candidates
        .into_iter()
        .filter(|t| seen.insert(t.clone()))
        .collect();

    let db_path = crate::data_routes::workspace_root()
        .join(".backup-dr")
        .join("evidence.sqlite3");
    let conn = open_readonly_sqlite(&db_path).ok().flatten();
    let now_iso = now_utc_iso();

    let mut available: Vec<(i32, String, String, String)> = Vec::new();
    for tid in unique {
        let mut pt = latest_recoverable_point(conn.as_ref(), &tid, policy_id, &now_iso)
            .ok()
            .flatten();
        if pt.is_none() {
            if let Ok(copies) =
                list_logical_recovery_copies(conn.as_ref(), Some(policy_id), None, None, 50)
            {
                pt = copies.into_iter().find(|c| {
                    truthy_str(c.get("targetId")) == tid
                        && c.get("recoverable").is_some_and(python_truthy)
                });
            }
        }
        let Some(rec) = pt else {
            continue;
        };
        let bid = truthy_str(rec.get("backupId"));
        if bid.is_empty() {
            continue;
        }
        let outcome = latest_drill_outcome(conn.as_ref(), &tid, policy_id, &now_iso)
            .ok()
            .flatten();
        let has_success = outcome
            .as_ref()
            .is_some_and(|o| truthy_str(o.get("result")) == "success");
        let observed = outcome
            .as_ref()
            .map(|o| {
                let oa = truthy_str(o.get("observedAt"));
                if !oa.is_empty() {
                    oa
                } else {
                    truthy_str(o.get("latestSuccessfulAt"))
                }
            })
            .unwrap_or_default();
        available.push((i32::from(has_success), observed, tid, bid));
    }
    available.sort_by(|a, b| (a.0, &a.1, &a.2).cmp(&(b.0, &b.1, &b.2)));
    available
        .into_iter()
        .next()
        .map(|(_, _, tid, bid)| (tid, bid))
}

fn execute_scheduled_drill(root: &Path, policy_id: &str) -> Result<Value, AppError> {
    let policies = list_policies_readonly(root)?;
    let policy = policies
        .into_iter()
        .find(|p| truthy_str(p.get("policyId")) == policy_id)
        .ok_or_else(|| AppError::not_found(format!("Backup policy {policy_id} not found")))?;

    let drill_conf = policy.get("recoveryDrill").and_then(Value::as_object);
    if !drill_conf
        .and_then(|d| d.get("enabled"))
        .is_some_and(python_truthy)
    {
        return Ok(json!({
            "status": "skipped",
            "reason": "drill-disabled",
            "policyId": policy_id,
        }));
    }

    let fallback_target = {
        let t = truthy_str(policy.get("targetId"));
        if t.is_empty() {
            "managed-local".into()
        } else {
            t
        }
    };
    let provider_name = drill_conf
        .and_then(|d| d.get("provider"))
        .filter(|v| python_truthy(v))
        .map(deepseek_policy::python_json::value_str)
        .unwrap_or_else(|| "default".into());
    let cred_ref = drill_conf
        .and_then(|d| d.get("credentialRef"))
        .filter(|v| python_truthy(v))
        .map(deepseek_policy::python_json::value_str)
        .unwrap_or_default();

    let supported_provider = provider_name == "default" || provider_name == "memory";
    let cred_slot_key = format!("recovery-credential:{cred_ref}");
    let (used_slot_key, acquired) = if !supported_provider || cred_ref.is_empty() {
        (None, None)
    } else if has_secret(&cred_slot_key) {
        (
            Some(cred_slot_key.clone()),
            consume_secret(&cred_slot_key, None).ok(),
        )
    } else if has_secret(&cred_ref) {
        (
            Some(cred_ref.to_string()),
            consume_secret(&cred_ref, None).ok(),
        )
    } else {
        (None, None)
    };
    if let (Some(slot), Some((k, sec))) = (used_slot_key, &acquired) {
        let _ = put_secret_bytes(&slot, k, sec.as_slice());
    }

    let Some((cred_kind, secret_bytes)) = acquired else {
        let err_msg = if !supported_provider {
            format!("No RecoveryCredentialProvider: provider '{provider_name}' is not registered")
        } else {
            "unlock-required: automated drill blocked without recovery credential".to_string()
        };
        let mut details = Map::new();
        details.insert("error".into(), Value::String(err_msg.clone()));
        details.insert("policyId".into(), Value::String(policy_id.into()));
        details.insert("workClass".into(), Value::String("recovery-drill".into()));
        let mut d_id = String::from("drill_blocked_");
        d_id.push_str(
            &policy_id
                .chars()
                .filter(|c| c.is_ascii_alphanumeric() || *c == '_' || *c == '-')
                .take(24)
                .collect::<String>(),
        );
        let _ = open_dr_ledger_write()?;
        record_drill_evidence(&DrillEvidenceRecord {
            drill_id: &d_id,
            target_id: &fallback_target,
            policy_id,
            backup_id: "",
            drill_kind: "scheduled",
            observed_at: &now_utc_iso(),
            result: "blocked",
            duration_ms: 0,
            details: Some(&details),
        })?;
        return Ok(json!({
            "status": "blocked",
            "reason": "unlock-required",
            "error": err_msg,
            "policyId": policy_id,
            "targetId": fallback_target,
            "backupId": "",
        }));
    };

    let Some((chosen_target, chosen_backup)) =
        select_drill_candidate_copy(&policy, policy_id, &fallback_target)
    else {
        return Err(AppError::not_found(
            "No policy recovery point found for scheduled drill",
        ));
    };

    let session = create_restore_from_target(root, &chosen_target, &chosen_backup, None, None)?;
    let restore_id = truthy_str(session.get("restoreId"));
    let mut s_data = read_remote_restore_session(root, &restore_id)?
        .ok_or_else(|| AppError::not_found("Remote restore session not found"))?;
    s_data.insert("policyId".into(), Value::String(policy_id.into()));
    s_data.insert("automatedDrill".into(), json!(true));
    let sp = remote_session_path(root, &restore_id)?;
    write_atomic_remote_session(&sp, &mut s_data, false, false, false)?;
    put_secret_bytes(&restore_id, &cred_kind, secret_bytes.as_slice())?;
    run_recovery_drill(root, &restore_id)
}
