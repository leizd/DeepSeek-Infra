//! Durable action journal claim/execution/reconciliation/verification/compensation
//! engine shared by `POST /api/workspace/resilience/execute` and `POST /api/workspace/resilience/waves/run`.

use std::collections::{BTreeSet, HashMap, HashSet};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::time::Instant;

use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::backup_projection::sha256_bytes;
use deepseek_policy::backup_unlock::is_plain_sha256;
use deepseek_policy::core_utils::python_truthy;
use deepseek_policy::python_json::{dumps_compact, value_str};
use rusqlite::{Connection, OptionalExtension};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use crate::backup_capacity_routes::probe_target_capacity_with_probe;
use crate::backup_catalog_routes::{list_registered_targets, registered_target, utc_iso_from_unix};
use crate::backup_catalog_write_routes::acquire_target_writer;
use crate::backup_commit::{GENESIS_COMMIT_HASH, commit_hash, latest_commit};
use crate::backup_dr_audit_routes::{
    StageSampleRecord, open_dr_ledger_write, record_stage_sample, resolve_target_root,
};
use crate::backup_dr_slo_routes::load_drill_items;
use crate::backup_dr_status_routes::list_logical_recovery_copies;
use crate::backup_drill_routes::execute_run_dr_drill;
use crate::backup_restore_write_routes::write_atomic_json;
use crate::resilience_advisory_routes::list_policies_readonly;
use crate::resilience_state_support::{
    NEVER_AUTONOMOUS, conflict_error, dr_db_path, evaluate_risk_snapshot_readonly,
    get_action_from_conn, is_action_autonomous, new_hex_id, now_unix_secs_and_iso,
    observe_risk_snapshot_and_slo, open_journal_rw, parse_utc_epoch, read_action_rate_limits,
    read_autonomous_action_policy, severity_rank, try_record_slo_burn_rw, try_record_slo_sample_rw,
};
use crate::route_support::unhandled_error;

const ACTIVE_STATES: [&str; 5] = [
    "CLAIMED",
    "EXECUTING",
    "RECONCILING",
    "VERIFYING",
    "ASSESSING_EFFECT",
];

const TERMINAL_STATES: [&str; 9] = [
    "SUCCEEDED",
    "COMPENSATED",
    "COMPENSATION_REQUIRED",
    "FAILED_BEFORE_EFFECT",
    "NEEDS_OPERATOR",
    "EFFECT_UNKNOWN",
    "BLOCKED",
    "SKIPPED_NO_LONGER_NEEDED",
    "PREEMPTED",
];

const CHUNK_SIZE: usize = 65_536;

#[derive(Default)]
pub(crate) struct ActionStateUpdate<'a> {
    pub execution_epoch: Option<i64>,
    pub claim_token: Option<&'a str>,
    pub owner_instance_id: Option<&'a str>,
    pub lease_until: Option<&'a str>,
    pub effect_class: Option<&'a str>,
    pub compensation_state: Option<&'a str>,
    pub effect_handle: Option<Value>,
    pub result: Option<Value>,
    pub verification: Option<Value>,
    pub proof: Option<Value>,
    pub risk_before_digest: Option<&'a str>,
    pub risk_after_digest: Option<&'a str>,
    pub error: Option<&'a str>,
}

struct SourceHoldGuard {
    journal_hold: PathBuf,
    target_hold: PathBuf,
}

impl Drop for SourceHoldGuard {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.journal_hold);
        let _ = fs::remove_file(&self.target_hold);
    }
}

struct TargetWriterGuard {
    lease: deepseek_policy::backup_writer_lease::TargetWriterLease,
}

impl Drop for TargetWriterGuard {
    fn drop(&mut self) {
        self.lease.release();
    }
}

fn forbidden_error(msg: impl Into<String>) -> AppError {
    AppError {
        status: 403,
        code: codes::FORBIDDEN,
        message: msg.into(),
    }
}

fn not_found_error(msg: impl Into<String>) -> AppError {
    AppError {
        status: 404,
        code: codes::NOT_FOUND,
        message: msg.into(),
    }
}

fn rate_limited_error(msg: impl Into<String>) -> AppError {
    AppError {
        status: 429,
        code: codes::FORBIDDEN,
        message: msg.into(),
    }
}

fn action_param_str(action: &Value, p_key: &str, a_keys: &[&str]) -> String {
    if let Some(params) = action.get("parameters").and_then(Value::as_object) {
        if let Some(val) = params.get(p_key).filter(|v| python_truthy(v)) {
            return value_str(val);
        }
    }
    for key in a_keys {
        if let Some(val) = action.get(*key).filter(|v| python_truthy(v)) {
            return value_str(val);
        }
    }
    String::new()
}

fn ensure_locks_schema(conn: &Connection) -> Result<(), AppError> {
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS resilience_resource_locks (
            lock_key TEXT PRIMARY KEY,
            action_id TEXT NOT NULL,
            owner_instance_id TEXT,
            acquired_at TEXT NOT NULL,
            lease_until TEXT NOT NULL
         );
         CREATE INDEX IF NOT EXISTS idx_resilience_locks_action
            ON resilience_resource_locks(action_id);",
    )
    .map_err(|_| unhandled_error())?;
    Ok(())
}

fn derive_resource_locks_for_action(action: &Value) -> Vec<String> {
    let pid = action_param_str(action, "policyId", &["policyId"]);
    let bid = action_param_str(action, "backupId", &["backupId"]);
    let src = action_param_str(action, "sourceTargetId", &["source"]);
    let dst = action_param_str(action, "destTargetId", &["destination", "target"]);
    let mut locks = BTreeSet::new();
    if !pid.is_empty() && !bid.is_empty() {
        locks.insert(format!("backup:{pid}:{bid}"));
    }
    if !src.is_empty() {
        locks.insert(format!("target:{src}"));
    }
    if !dst.is_empty() && dst != src {
        locks.insert(format!("target:{dst}"));
    }
    if !pid.is_empty() && bid.is_empty() {
        locks.insert(format!("policy:{pid}"));
    }
    locks.into_iter().collect()
}

fn acquire_action_locks(
    conn: &Connection,
    action_id: &str,
    lock_keys: &[String],
    owner_instance_id: &str,
    lease_until: &str,
    now_iso: &str,
) -> Result<Result<(), String>, AppError> {
    ensure_locks_schema(conn)?;
    for key in lock_keys {
        let row: Option<(String, String)> = conn
            .query_row(
                "SELECT action_id, lease_until FROM resilience_resource_locks WHERE lock_key = ?",
                [key],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()
            .map_err(|_| unhandled_error())?;
        if let Some((held_id, held_until)) = row {
            if held_id != action_id && held_until.as_str() > now_iso {
                return Ok(Err(format!(
                    "resource-locked:{key}:held-by:{held_id}:until:{held_until}"
                )));
            }
        }
    }
    for key in lock_keys {
        conn.execute(
            "INSERT INTO resilience_resource_locks
                (lock_key, action_id, owner_instance_id, acquired_at, lease_until)
             VALUES (?, ?, ?, ?, ?)
             ON CONFLICT(lock_key) DO UPDATE SET
                action_id = excluded.action_id,
                owner_instance_id = excluded.owner_instance_id,
                acquired_at = excluded.acquired_at,
                lease_until = excluded.lease_until",
            rusqlite::params![key, action_id, owner_instance_id, now_iso, lease_until],
        )
        .map_err(|_| unhandled_error())?;
    }
    Ok(Ok(()))
}

fn release_action_locks(conn: &Connection, action_id: &str) -> Result<(), AppError> {
    ensure_locks_schema(conn)?;
    conn.execute(
        "DELETE FROM resilience_resource_locks WHERE action_id = ?",
        [action_id],
    )
    .map_err(|_| unhandled_error())?;
    Ok(())
}

struct ActionEvent<'a> {
    action_id: &'a str,
    event_type: &'a str,
    state: &'a str,
    owner_instance_id: Option<&'a str>,
    execution_epoch: i64,
    claim_token: Option<&'a str>,
    effect_handle: Option<&'a Value>,
    created_at: &'a str,
}

fn append_action_event(conn: &Connection, ev: &ActionEvent<'_>) -> Result<(), AppError> {
    let token_digest = ev.claim_token.map(|t| sha256_bytes(t.as_bytes()));
    let handle_json = ev.effect_handle.map(dumps_compact);
    conn.execute(
        "INSERT INTO resilience_action_events (
            action_id, event_type, state, owner_instance_id, execution_epoch,
            claim_token_sha256, effect_handle_json, created_at
         ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rusqlite::params![
            ev.action_id,
            ev.event_type,
            ev.state,
            ev.owner_instance_id,
            ev.execution_epoch,
            token_digest,
            handle_json,
            ev.created_at,
        ],
    )
    .map_err(|_| unhandled_error())?;
    Ok(())
}

pub(crate) fn record_action_intent_db(
    root: &Path,
    action: &Value,
    created_by: &str,
    plan_id: Option<&str>,
    input_risk_digest: &str,
    plan_digest: &str,
    now_iso: &str,
) -> Result<Value, AppError> {
    let action_id = action
        .get("actionId")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| format!("act_{}", &new_hex_id()[..12]));
    let act_type = action
        .get("type")
        .filter(|v| python_truthy(v))
        .map(|v| value_str(v).to_uppercase())
        .unwrap_or_default();
    let req_approval = action.get("requiresApproval").is_some_and(python_truthy);
    let policy = read_autonomous_action_policy(root);
    let is_auto = is_action_autonomous(&act_type, &policy);
    let initial_state = if req_approval || !is_auto {
        "APPROVAL_REQUIRED"
    } else {
        "PENDING"
    };
    let eff_plan_id = plan_id
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .or_else(|| {
            action
                .get("planId")
                .filter(|v| python_truthy(v))
                .map(value_str)
        });
    let params = action
        .get("parameters")
        .filter(|v| v.is_object())
        .cloned()
        .unwrap_or_else(|| json!({}));
    let risk_subj = action.get("riskSubject").cloned().unwrap_or(Value::Null);
    let expected_effect = action
        .get("expectedEffect")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_else(|| "severity-decrease".to_string());
    let sev_before = action
        .get("severityBefore")
        .filter(|v| python_truthy(v))
        .map(value_str);
    let coord_id = action
        .get("coordinationPlanId")
        .filter(|v| python_truthy(v))
        .map(value_str);

    let conn = open_journal_rw(root)?;
    let existing: Option<Option<String>> = conn
        .query_row(
            "SELECT parameters_json FROM resilience_actions WHERE action_id = ?",
            [&action_id],
            |r| r.get(0),
        )
        .optional()
        .map_err(|_| unhandled_error())?;

    if let Some(opt_params) = existing {
        let existing_params = opt_params
            .and_then(|s| serde_json::from_str::<Value>(&s).ok())
            .unwrap_or_else(|| json!({}));
        if existing_params != params {
            return Err(conflict_error(format!(
                "Action identity conflict: action '{action_id}' exists with different parameters"
            )));
        }
        return Ok(get_action_from_conn(&conn, &action_id)?.unwrap_or_else(|| json!({})));
    }

    let params_json = serde_json::to_string(&params).map_err(|_| unhandled_error())?;
    let risk_subj_json = if risk_subj.is_null() {
        None
    } else {
        Some(serde_json::to_string(&risk_subj).map_err(|_| unhandled_error())?)
    };
    conn.execute(
        "INSERT INTO resilience_actions (
            action_id, plan_id, action_type, created_by, input_risk_digest, plan_digest,
            state, parameters_json, owner_instance_id, lease_until, claim_token, execution_epoch,
            effect_class, compensation_state, effect_handle_json, risk_subject_json, expected_effect,
            severity_before, coordination_plan_id, execution_result_json, verification_result_json,
            decision_proof_json, risk_before_digest, risk_after_digest, error_message,
            created_at, updated_at
         ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, 0, 'NO_EFFECT', NULL, NULL, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)",
        rusqlite::params![
            action_id,
            eff_plan_id,
            act_type,
            created_by,
            input_risk_digest,
            plan_digest,
            initial_state,
            params_json,
            risk_subj_json,
            expected_effect,
            sev_before,
            coord_id,
            now_iso,
            now_iso,
        ],
    )
    .map_err(|_| unhandled_error())?;
    Ok(get_action_from_conn(&conn, &action_id)?.unwrap_or_else(|| json!({})))
}

pub(crate) fn update_action_state_db(
    root: &Path,
    action_id: &str,
    state: &str,
    opts: &ActionStateUpdate<'_>,
) -> Result<Value, AppError> {
    let (now_secs, now_iso) = now_unix_secs_and_iso();
    let conn = open_journal_rw(root)?;
    let existing = get_action_from_conn(&conn, action_id)?
        .ok_or_else(|| not_found_error(format!("Action '{action_id}' not found in journal")))?;

    let action_type = existing
        .get("type")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_uppercase();
    let prev_state = existing
        .get("state")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let stored_epoch = existing
        .get("executionEpoch")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let recorded_epoch = opts.execution_epoch.unwrap_or(stored_epoch);
    let started_at: Option<String> = conn
        .query_row(
            "SELECT created_at FROM resilience_action_events
             WHERE action_id = ? AND event_type IN ('ACTION_CLAIMED', 'ACTION_TAKEOVER')
             ORDER BY event_id ASC LIMIT 1",
            [action_id],
            |r| r.get(0),
        )
        .optional()
        .map_err(|_| unhandled_error())?;

    let new_handle = opts.effect_handle.clone().or_else(|| {
        existing
            .get("effectHandle")
            .filter(|v| !v.is_null())
            .cloned()
    });
    let new_result = opts.result.clone().or_else(|| {
        existing
            .get("executionResult")
            .filter(|v| !v.is_null())
            .cloned()
    });
    let new_verif = opts.verification.clone().or_else(|| {
        existing
            .get("verificationResult")
            .filter(|v| !v.is_null())
            .cloned()
    });
    let new_proof = opts.proof.clone().or_else(|| {
        existing
            .get("decisionProof")
            .filter(|v| !v.is_null())
            .cloned()
    });

    let ser_opt = |opt: &Option<Value>| -> Result<Option<String>, AppError> {
        match opt {
            Some(v) if !v.is_null() => Ok(Some(
                serde_json::to_string(v).map_err(|_| unhandled_error())?,
            )),
            _ => Ok(None),
        }
    };
    let h_json = ser_opt(&new_handle)?;
    let r_json = ser_opt(&new_result)?;
    let v_json = ser_opt(&new_verif)?;
    let p_json = ser_opt(&new_proof)?;

    if let (Some(epoch), Some(token)) = (opts.execution_epoch, opts.claim_token) {
        let updated = conn
            .execute(
                "UPDATE resilience_actions SET
                    state = ?,
                    owner_instance_id = COALESCE(?, owner_instance_id),
                    lease_until = COALESCE(?, lease_until),
                    effect_class = COALESCE(?, effect_class),
                    compensation_state = COALESCE(?, compensation_state),
                    effect_handle_json = ?,
                    execution_result_json = ?,
                    verification_result_json = ?,
                    decision_proof_json = ?,
                    risk_before_digest = COALESCE(?, risk_before_digest),
                    risk_after_digest = COALESCE(?, risk_after_digest),
                    error_message = ?,
                    updated_at = ?
                 WHERE action_id = ? AND execution_epoch = ? AND claim_token = ?",
                rusqlite::params![
                    state,
                    opts.owner_instance_id,
                    opts.lease_until,
                    opts.effect_class,
                    opts.compensation_state,
                    h_json,
                    r_json,
                    v_json,
                    p_json,
                    opts.risk_before_digest,
                    opts.risk_after_digest,
                    opts.error,
                    now_iso,
                    action_id,
                    epoch,
                    token,
                ],
            )
            .map_err(|_| unhandled_error())?;
        if updated == 0 {
            return Err(conflict_error(format!(
                "Action '{action_id}' lease lost (CAS epoch {epoch} token mismatch): stale worker cannot commit"
            )));
        }
    } else {
        conn.execute(
            "UPDATE resilience_actions SET
                state = ?,
                owner_instance_id = COALESCE(?, owner_instance_id),
                lease_until = COALESCE(?, lease_until),
                claim_token = COALESCE(?, claim_token),
                effect_class = COALESCE(?, effect_class),
                compensation_state = COALESCE(?, compensation_state),
                effect_handle_json = ?,
                execution_result_json = ?,
                verification_result_json = ?,
                decision_proof_json = ?,
                risk_before_digest = COALESCE(?, risk_before_digest),
                risk_after_digest = COALESCE(?, risk_after_digest),
                error_message = ?,
                updated_at = ?
             WHERE action_id = ?",
            rusqlite::params![
                state,
                opts.owner_instance_id,
                opts.lease_until,
                opts.claim_token,
                opts.effect_class,
                opts.compensation_state,
                h_json,
                r_json,
                v_json,
                p_json,
                opts.risk_before_digest,
                opts.risk_after_digest,
                opts.error,
                now_iso,
                action_id,
            ],
        )
        .map_err(|_| unhandled_error())?;
    }

    let is_term = TERMINAL_STATES.contains(&state);
    if is_term {
        release_action_locks(&conn, action_id)?;
    }

    let ev_owner = opts.owner_instance_id.map(str::to_string).or_else(|| {
        existing
            .get("ownerInstanceId")
            .and_then(Value::as_str)
            .map(str::to_string)
    });
    let ev_token = opts.claim_token.map(str::to_string).or_else(|| {
        existing
            .get("claimToken")
            .and_then(Value::as_str)
            .map(str::to_string)
    });
    append_action_event(
        &conn,
        &ActionEvent {
            action_id,
            event_type: "STATE_TRANSITION",
            state,
            owner_instance_id: ev_owner.as_deref(),
            execution_epoch: recorded_epoch,
            claim_token: ev_token.as_deref(),
            effect_handle: new_handle.as_ref(),
            created_at: &now_iso,
        },
    )?;

    let out = get_action_from_conn(&conn, action_id)?.unwrap_or_else(|| json!({}));
    drop(conn);

    if is_term
        && !TERMINAL_STATES.contains(&prev_state.as_str())
        && (action_type == "CREATE_REPAIR_JOB" || action_type == "CREATE_REBALANCE_JOB")
    {
        if let Some(started_ep) = parse_utc_epoch(started_at.as_deref()) {
            let duration_ms = (((now_secs as i64 - started_ep) as f64) * 1000.0).max(0.0);
            let metric = if action_type == "CREATE_REPAIR_JOB" {
                "repair_time_ms"
            } else {
                "rebalance_time_ms"
            };
            let success = state == "SUCCEEDED";
            try_record_slo_sample_rw(
                root,
                metric,
                duration_ms,
                &now_iso,
                None,
                &format!("remediation:{action_id}:{recorded_epoch}"),
            );
            try_record_slo_burn_rw(
                root,
                "failed_remediation_ratio",
                if success { 0.0 } else { 1.0 },
                1.0,
                &now_iso,
                &now_iso,
                &format!("remediation-outcome:{action_id}:{recorded_epoch}"),
            );
        }
    }
    Ok(out)
}

fn check_rate_limits_in_tx(
    conn: &Connection,
    root: &Path,
    action: &Value,
    exclude_action_id: &str,
    now_secs: u64,
    now_iso: &str,
) -> Result<(bool, String), AppError> {
    let limits = read_action_rate_limits(root);
    let max_concurrent = limits
        .get("maxConcurrentActions")
        .and_then(Value::as_i64)
        .unwrap_or(3);
    let active_count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM resilience_actions
             WHERE state IN ('CLAIMED', 'EXECUTING', 'RECONCILING', 'VERIFYING', 'ASSESSING_EFFECT')
               AND action_id != ?",
            [exclude_action_id],
            |r| r.get(0),
        )
        .map_err(|_| unhandled_error())?;

    if active_count >= max_concurrent {
        let act_type = action
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let act_sev = action
            .get("severity")
            .or_else(|| action.get("severityBefore"))
            .filter(|v| python_truthy(v))
            .map(|v| value_str(v).to_lowercase())
            .unwrap_or_default();
        if act_type == "CREATE_REPAIR_JOB" && act_sev == "critical" {
            struct PreemptVictimRow {
                action_id: String,
                state: String,
                effect_class: Option<String>,
                execution_epoch: i64,
                owner_instance_id: Option<String>,
                claim_token: Option<String>,
                effect_handle_json: Option<String>,
            }
            let victim: Option<PreemptVictimRow> = conn
                .query_row(
                    "SELECT action_id, state, effect_class, execution_epoch,
                            owner_instance_id, claim_token, effect_handle_json
                     FROM resilience_actions
                     WHERE action_type = 'CREATE_REBALANCE_JOB'
                       AND state = 'CLAIMED'
                       AND LOWER(COALESCE(severity_before, 'warning')) = 'warning'
                     ORDER BY created_at ASC LIMIT 1",
                    [],
                    |r| {
                        Ok(PreemptVictimRow {
                            action_id: r.get(0)?,
                            state: r.get(1)?,
                            effect_class: r.get(2)?,
                            execution_epoch: r.get(3)?,
                            owner_instance_id: r.get(4)?,
                            claim_token: r.get(5)?,
                            effect_handle_json: r.get(6)?,
                        })
                    },
                )
                .optional()
                .map_err(|_| unhandled_error())?;
            if let Some(v) = victim {
                let v_eff_str = v.effect_class.as_deref().unwrap_or("");
                if v.state == "CLAIMED" && (v_eff_str.is_empty() || v_eff_str == "NO_EFFECT") {
                    let updated = conn
                        .execute(
                            "UPDATE resilience_actions
                             SET state = 'PREEMPTED', error_message = 'preempted-by-critical-repair', updated_at = ?
                             WHERE action_id = ? AND state = 'CLAIMED'
                               AND COALESCE(effect_class, 'NO_EFFECT') IN ('', 'NO_EFFECT')",
                            rusqlite::params![now_iso, v.action_id],
                        )
                        .map_err(|_| unhandled_error())?;
                    if updated == 1 {
                        release_action_locks(conn, &v.action_id)?;
                        let preemptor_id =
                            action.get("actionId").map(value_str).unwrap_or_default();
                        let eff_label = if v_eff_str.is_empty() {
                            "NO_EFFECT"
                        } else {
                            v_eff_str
                        };
                        conn.execute(
                            "INSERT INTO resilience_preemption_decisions (
                                preemptor_action_id, victim_action_id, victim_state,
                                victim_effect_class, safe, reason, created_at
                             ) VALUES (?, ?, ?, ?, 1, 'critical-repair-preempts-warning-rebalance', ?)",
                            rusqlite::params![preemptor_id, v.action_id, v.state, eff_label, now_iso],
                        )
                        .map_err(|_| unhandled_error())?;
                        let v_handle_val = v
                            .effect_handle_json
                            .as_deref()
                            .and_then(|s| serde_json::from_str::<Value>(s).ok());
                        append_action_event(
                            conn,
                            &ActionEvent {
                                action_id: &v.action_id,
                                event_type: "SAFE_PREEMPTION",
                                state: "PREEMPTED",
                                owner_instance_id: v.owner_instance_id.as_deref(),
                                execution_epoch: v.execution_epoch,
                                claim_token: v.claim_token.as_deref(),
                                effect_handle: v_handle_val.as_ref(),
                                created_at: now_iso,
                            },
                        )?;
                        return Ok((true, "admitted-with-preemption".to_string()));
                    }
                }
            }
        }
        return Ok((
            false,
            format!("max-concurrent-actions-exceeded:{active_count}>={max_concurrent}"),
        ));
    }

    // Active rows scan for per-target, per-policy, per-domain limits
    let mut stmt = conn
        .prepare(
            "SELECT parameters_json, risk_subject_json FROM resilience_actions
             WHERE state IN ('CLAIMED', 'EXECUTING', 'RECONCILING', 'VERIFYING', 'ASSESSING_EFFECT')
               AND action_id != ?",
        )
        .map_err(|_| unhandled_error())?;
    let active_rows: Vec<(Value, Value)> = stmt
        .query_map([exclude_action_id], |r| {
            let pj: Option<String> = r.get(0)?;
            let sj: Option<String> = r.get(1)?;
            let p_val = pj
                .as_deref()
                .and_then(|s| serde_json::from_str::<Value>(s).ok())
                .unwrap_or_else(|| json!({}));
            let s_val = sj
                .as_deref()
                .and_then(|s| serde_json::from_str::<Value>(s).ok())
                .unwrap_or_else(|| json!({}));
            Ok((p_val, s_val))
        })
        .map_err(|_| unhandled_error())?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|_| unhandled_error())?;
    drop(stmt);

    let src = action_param_str(action, "sourceTargetId", &["source"]);
    let dst = action_param_str(action, "destTargetId", &["destination", "target"]);
    let target_limit = limits
        .get("maxConcurrentPerTarget")
        .and_then(Value::as_i64)
        .unwrap_or(2);
    for tid in [src, dst] {
        if tid.is_empty() {
            continue;
        }
        let target_active = active_rows
            .iter()
            .filter(|(p, _)| {
                p.get("sourceTargetId").and_then(Value::as_str) == Some(&tid)
                    || p.get("destTargetId").and_then(Value::as_str) == Some(&tid)
                    || p.get("targetId").and_then(Value::as_str) == Some(&tid)
            })
            .count() as i64;
        if target_active >= target_limit {
            return Ok((
                false,
                format!(
                    "max-per-target-concurrent-actions-exceeded:{tid}:{target_active}>={target_limit}"
                ),
            ));
        }
    }

    let pid = {
        let p = action_param_str(action, "policyId", &["policyId"]);
        if p.is_empty() {
            action
                .get("riskSubject")
                .and_then(|rs| rs.get("policyId"))
                .filter(|v| python_truthy(v))
                .map(value_str)
                .unwrap_or_default()
        } else {
            p
        }
    };
    let policy_limit = limits
        .get("maxConcurrentPerPolicy")
        .and_then(Value::as_i64)
        .unwrap_or(2);
    if !pid.is_empty() {
        let policy_active = active_rows
            .iter()
            .filter(|(p, s)| {
                let ap = p
                    .get("policyId")
                    .or_else(|| s.get("policyId"))
                    .filter(|v| python_truthy(v))
                    .map(value_str)
                    .unwrap_or_default();
                ap == pid
            })
            .count() as i64;
        if policy_active >= policy_limit {
            return Ok((
                false,
                format!(
                    "max-per-policy-concurrent-actions-exceeded:{pid}:{policy_active}>={policy_limit}"
                ),
            ));
        }
    }

    let fd = action
        .get("parameters")
        .and_then(|p| p.get("failureDomain"))
        .or_else(|| {
            action
                .get("riskSubject")
                .and_then(|s| s.get("failureDomain"))
        })
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();
    let domain_limit = limits
        .get("maxSimultaneousFailureDomainsTouched")
        .and_then(Value::as_i64)
        .unwrap_or(1) as usize;
    if !fd.is_empty() {
        let mut active_domains = HashSet::new();
        for (p, s) in &active_rows {
            if let Some(d) = p
                .get("failureDomain")
                .or_else(|| s.get("failureDomain"))
                .filter(|v| python_truthy(v))
                .map(value_str)
                .filter(|x| !x.is_empty())
            {
                active_domains.insert(d);
            }
        }
        if !active_domains.contains(&fd) && active_domains.len() >= domain_limit {
            return Ok((
                false,
                format!(
                    "max-failure-domains-touched-exceeded:{fd}:{}>={domain_limit}",
                    active_domains.len()
                ),
            ));
        }
    }

    let one_hour_ago = utc_iso_from_unix(now_secs.saturating_sub(3600));
    let max_hourly = limits
        .get("maxActionsPerHour")
        .and_then(Value::as_i64)
        .unwrap_or(20);
    let hourly_count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM resilience_actions
             WHERE created_at >= ? AND action_id != ?
               AND state NOT IN ('BLOCKED', 'STALE', 'SKIPPED_NO_LONGER_NEEDED', 'PREEMPTED')",
            rusqlite::params![one_hour_ago, exclude_action_id],
            |r| r.get(0),
        )
        .map_err(|_| unhandled_error())?;
    if hourly_count >= max_hourly {
        return Ok((
            false,
            format!("max-actions-per-hour-exceeded:{hourly_count}>={max_hourly}"),
        ));
    }

    Ok((true, "admitted".to_string()))
}

fn admit_and_claim_action_db(
    root: &Path,
    action_id: &str,
    owner_instance_id: &str,
    lease_seconds: u64,
) -> Result<(bool, Option<Value>, String), AppError> {
    let (now_secs, now_iso) = now_unix_secs_and_iso();
    let lease_until_iso = utc_iso_from_unix(now_secs.saturating_add(lease_seconds));
    let claim_token = new_hex_id()[..32].to_string();

    let conn = open_journal_rw(root)?;
    let Some(action) = get_action_from_conn(&conn, action_id)? else {
        return Ok((false, None, "action-not-found".to_string()));
    };
    let lock_keys = derive_resource_locks_for_action(&action);

    conn.execute_batch("BEGIN IMMEDIATE;")
        .map_err(|_| unhandled_error())?;

    let tx_res: Result<(bool, String, String, String), AppError> = (|| {
        let cur_row: Option<(String, Option<String>, String)> = conn
            .query_row(
                "SELECT state, lease_until, created_at FROM resilience_actions WHERE action_id = ?",
                [action_id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .optional()
            .map_err(|_| unhandled_error())?;
        let Some((current_state, current_lease, created_at)) = cur_row else {
            return Ok((
                false,
                "action-not-found".to_string(),
                String::new(),
                String::new(),
            ));
        };
        let lease_str = current_lease.unwrap_or_default();
        let claimable = current_state == "PENDING"
            || (ACTIVE_STATES.contains(&current_state.as_str())
                && lease_str.as_str() < now_iso.as_str());
        if !claimable {
            return Ok((
                false,
                "claim-rejected-not-pending-or-active-lease".to_string(),
                current_state,
                created_at,
            ));
        }
        let claimed_state = if current_state == "PENDING" {
            "CLAIMED".to_string()
        } else {
            "RECONCILING".to_string()
        };
        let (rate_ok, rate_reason) =
            check_rate_limits_in_tx(&conn, root, &action, action_id, now_secs, &now_iso)?;
        if !rate_ok {
            return Ok((false, rate_reason, current_state, created_at));
        }
        let admission_reason = if rate_reason == "admitted-with-preemption" {
            rate_reason
        } else {
            "admitted-and-claimed".to_string()
        };
        if !lock_keys.is_empty() {
            if let Err(lock_reason) = acquire_action_locks(
                &conn,
                action_id,
                &lock_keys,
                owner_instance_id,
                &lease_until_iso,
                &now_iso,
            )? {
                return Ok((false, lock_reason, current_state, created_at));
            }
        }
        let updated = conn
            .execute(
                "UPDATE resilience_actions
                 SET state = ?, owner_instance_id = ?, lease_until = ?, claim_token = ?,
                     execution_epoch = execution_epoch + 1, updated_at = ?
                 WHERE action_id = ?
                   AND (
                       state = 'PENDING'
                       OR (state IN ('CLAIMED', 'EXECUTING', 'RECONCILING', 'VERIFYING', 'ASSESSING_EFFECT') AND lease_until < ?)
                   )",
                rusqlite::params![
                    claimed_state,
                    owner_instance_id,
                    lease_until_iso,
                    claim_token,
                    now_iso,
                    action_id,
                    now_iso,
                ],
            )
            .map_err(|_| unhandled_error())?;
        if updated != 1 {
            return Ok((
                false,
                "claim-rejected-not-pending-or-active-lease".to_string(),
                current_state,
                created_at,
            ));
        }
        let new_epoch = action
            .get("executionEpoch")
            .and_then(Value::as_i64)
            .unwrap_or(0)
            + 1;
        let handle_val = action.get("effectHandle").filter(|v| v.is_object());
        let ev_type = if claimed_state == "CLAIMED" {
            "ACTION_CLAIMED"
        } else {
            "ACTION_TAKEOVER"
        };
        append_action_event(
            &conn,
            &ActionEvent {
                action_id,
                event_type: ev_type,
                state: &claimed_state,
                owner_instance_id: Some(owner_instance_id),
                execution_epoch: new_epoch,
                claim_token: Some(&claim_token),
                effect_handle: handle_val,
                created_at: &now_iso,
            },
        )?;
        Ok((
            true,
            admission_reason,
            claimed_state,
            format!("{created_at}|{lease_str}"),
        ))
    })();

    let (claimed, reason, state_tag, ts_meta) = match tx_res {
        Ok(res) => res,
        Err(err) => {
            let _ = conn.execute_batch("ROLLBACK;");
            return Err(err);
        }
    };
    if !claimed {
        let _ = conn.execute_batch("ROLLBACK;");
        let current = get_action_from_conn(&conn, action_id)?;
        return Ok((false, current, reason));
    }
    conn.execute_batch("COMMIT;")
        .map_err(|_| unhandled_error())?;

    let refreshed = get_action_from_conn(&conn, action_id)?;
    drop(conn);

    let claimed_epoch = action
        .get("executionEpoch")
        .and_then(Value::as_i64)
        .unwrap_or(0)
        + 1;
    let mut parts = ts_meta.splitn(2, '|');
    let created_str = parts.next().unwrap_or("");
    let prev_lease_str = parts.next().unwrap_or("");
    if state_tag == "CLAIMED" {
        if let Some(c_ep) = parse_utc_epoch(Some(created_str)) {
            let q_ms = (((now_secs as i64 - c_ep) as f64) * 1000.0).max(0.0);
            try_record_slo_sample_rw(
                root,
                "remediation_queue_delay_ms",
                q_ms,
                &now_iso,
                None,
                &format!("queue-delay:{action_id}:{claimed_epoch}"),
            );
            let thresh = action
                .get("queueDelaySloMs")
                .and_then(Value::as_f64)
                .unwrap_or(300_000.0);
            try_record_slo_burn_rw(
                root,
                "queue_delay_violations",
                if q_ms > thresh { 1.0 } else { 0.0 },
                1.0,
                &now_iso,
                &now_iso,
                &format!("queue-delay-outcome:{action_id}:{claimed_epoch}"),
            );
        }
    } else if let Some(l_ep) = parse_utc_epoch(Some(prev_lease_str)) {
        let t_ms = (((now_secs as i64 - l_ep) as f64) * 1000.0).max(0.0);
        try_record_slo_sample_rw(
            root,
            "lease_takeover_time_ms",
            t_ms,
            &now_iso,
            None,
            &format!("lease-takeover:{action_id}:{claimed_epoch}"),
        );
    }

    Ok((true, refreshed, reason))
}

fn validate_action_admission(root: &Path, action: &Value) -> Result<(), String> {
    let act_type = action
        .get("type")
        .filter(|v| python_truthy(v))
        .map(|v| value_str(v).to_uppercase())
        .unwrap_or_default();
    if act_type.is_empty() {
        return Err("missing-action-type".to_string());
    }
    let approved = action.get("approved").is_some_and(python_truthy);
    if NEVER_AUTONOMOUS.contains(&act_type.as_str()) && !approved {
        return Err(format!("action-{act_type}-forbidden-by-safety-floor"));
    }
    let policy = read_autonomous_action_policy(root);
    let req_appr = action.get("requiresApproval").is_some_and(python_truthy);
    let is_auto = is_action_autonomous(&act_type, &policy);
    if req_appr && is_auto {
        return Err(format!("action-{act_type}-requires-explicit-approval"));
    }
    if !is_auto && !approved {
        return Err(format!(
            "action-{act_type}-not-autonomous-approval-required"
        ));
    }
    Ok(())
}

fn read_json_dir_records(dir: &Path) -> Vec<Value> {
    if !dir.is_dir() {
        return Vec::new();
    }
    let mut paths: Vec<PathBuf> = fs::read_dir(dir)
        .into_iter()
        .flatten()
        .flatten()
        .map(|e| e.path())
        .filter(|p| {
            p.extension().and_then(|s| s.to_str()) == Some("json")
                && !p
                    .file_name()
                    .and_then(|s| s.to_str())
                    .is_some_and(|n| n.starts_with('.'))
        })
        .collect();
    paths.sort();
    paths.reverse();
    paths
        .into_iter()
        .filter_map(|p| {
            let text = fs::read_to_string(&p).ok()?;
            serde_json::from_str::<Value>(&text)
                .ok()
                .filter(Value::is_object)
        })
        .collect()
}

pub(crate) fn read_repair_job_fs(root: &Path, repair_id: &str) -> Option<Value> {
    let p = root
        .join(".backup-replication")
        .join("repairs")
        .join(format!("{repair_id}.json"));
    let text = fs::read_to_string(&p).ok()?;
    serde_json::from_str::<Value>(&text)
        .ok()
        .filter(Value::is_object)
}

pub(crate) fn read_rebalance_job_fs(root: &Path, job_id: &str) -> Option<Value> {
    let p = root
        .join(".backup-rebalance")
        .join(format!("{job_id}.json"));
    let text = fs::read_to_string(&p).ok()?;
    serde_json::from_str::<Value>(&text)
        .ok()
        .filter(Value::is_object)
}

enum ReconcileDirective {
    ResumeSimulating,
    RecreateEffect,
    ResumeExecution(Value),
    AdvanceToVerifying(Value),
    TriggerCompensation(Value),
    EffectUnknown(Value),
}

fn reconcile_action_effect_db(root: &Path, action: &Value) -> ReconcileDirective {
    let act_type = action
        .get("type")
        .filter(|v| python_truthy(v))
        .map(|v| value_str(v).to_uppercase())
        .unwrap_or_default();
    let act_id = action
        .get("actionId")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();
    let handle = action.get("effectHandle").and_then(Value::as_object);
    let effect_class = action
        .get("effectClass")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let cur_state = action
        .get("state")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_uppercase();
    if cur_state == "CLAIMED" {
        return ReconcileDirective::ResumeSimulating;
    }

    if act_type == "CREATE_REPAIR_JOB" {
        let repair_id = handle
            .and_then(|h| h.get("repairId"))
            .filter(|v| python_truthy(v))
            .map(value_str);
        let mut job = repair_id
            .as_deref()
            .and_then(|id| read_repair_job_fs(root, id));
        if job.is_none() {
            let all = read_json_dir_records(&root.join(".backup-replication").join("repairs"));
            job = all
                .into_iter()
                .find(|r| r.get("resilienceActionId").and_then(Value::as_str) == Some(&act_id));
        }
        let Some(j) = job else {
            if repair_id.is_some() || (!effect_class.is_empty() && effect_class != "NO_EFFECT") {
                return ReconcileDirective::EffectUnknown(json!({
                    "error": "repair-effect-handle-not-observed",
                    "repairId": repair_id,
                }));
            }
            return ReconcileDirective::RecreateEffect;
        };
        let phase = j
            .get("phase")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_lowercase();
        let r_id = j.get("repairId").cloned().unwrap_or(Value::Null);
        if phase == "complete" || phase == "healthy" {
            return ReconcileDirective::AdvanceToVerifying(json!({"job": j, "repairId": r_id}));
        }
        if phase == "failed" || phase == "failed-terminal" {
            let err = j
                .get("error")
                .filter(|v| python_truthy(v))
                .map(value_str)
                .unwrap_or_else(|| "repair-job-failed".to_string());
            return ReconcileDirective::TriggerCompensation(json!({"error": err, "job": j}));
        }
        return ReconcileDirective::ResumeExecution(json!({"job": j, "repairId": r_id}));
    }

    if act_type == "CREATE_REBALANCE_JOB" {
        let job_id = handle
            .and_then(|h| h.get("jobId"))
            .filter(|v| python_truthy(v))
            .map(value_str);
        let mut job = job_id
            .as_deref()
            .and_then(|id| read_rebalance_job_fs(root, id));
        if job.is_none() {
            let all = read_json_dir_records(&root.join(".backup-rebalance"));
            job = all
                .into_iter()
                .find(|r| r.get("resilienceActionId").and_then(Value::as_str) == Some(&act_id));
        }
        let Some(j) = job else {
            if job_id.is_some() || (!effect_class.is_empty() && effect_class != "NO_EFFECT") {
                return ReconcileDirective::EffectUnknown(json!({
                    "error": "rebalance-effect-handle-not-observed",
                    "jobId": job_id,
                }));
            }
            return ReconcileDirective::RecreateEffect;
        };
        let phase = j
            .get("phase")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_lowercase();
        let j_id = j.get("jobId").cloned().unwrap_or(Value::Null);
        if phase == "complete" {
            return ReconcileDirective::AdvanceToVerifying(json!({"job": j, "jobId": j_id}));
        }
        if phase == "failed" {
            let err = j
                .get("error")
                .filter(|v| python_truthy(v))
                .map(value_str)
                .unwrap_or_else(|| "rebalance-job-failed".to_string());
            return ReconcileDirective::TriggerCompensation(json!({"error": err, "job": j}));
        }
        return ReconcileDirective::ResumeExecution(json!({"job": j, "jobId": j_id}));
    }

    if act_type == "START_DR_DRILL" {
        let drills = load_drill_items(root).unwrap_or_default();
        let drill_rec = drills.into_iter().find(|d| {
            d.get("resilienceActionId").and_then(Value::as_str) == Some(&act_id)
                || d.get("proof")
                    .and_then(|p| p.get("resilienceActionId"))
                    .and_then(Value::as_str)
                    == Some(&act_id)
        });
        let Some(dr) = drill_rec else {
            if handle.is_some_and(|h| !h.is_empty())
                || (!effect_class.is_empty() && effect_class != "NO_EFFECT")
            {
                return ReconcileDirective::EffectUnknown(
                    json!({"error": "drill-effect-handle-not-observed"}),
                );
            }
            return ReconcileDirective::RecreateEffect;
        };
        let ok = dr.get("result").and_then(Value::as_str) == Some("success")
            || dr.get("status").and_then(Value::as_str) == Some("success");
        if ok {
            let proof = dr.get("proof").cloned().unwrap_or_else(|| json!({}));
            let b_id = dr
                .get("backupId")
                .or_else(|| dr.get("testedBackupId"))
                .or_else(|| proof.get("backupId"))
                .cloned()
                .unwrap_or(Value::Null);
            return ReconcileDirective::AdvanceToVerifying(json!({
                "status": "success",
                "drillId": dr.get("drillId").cloned().unwrap_or(Value::Null),
                "backupId": b_id,
                "testedBackupId": b_id,
                "resilienceActionId": act_id,
                "proof": proof,
            }));
        }
        return ReconcileDirective::TriggerCompensation(json!({
            "error": dr.get("error").cloned().unwrap_or_else(|| json!("dr-drill-failed")),
            "drill": dr,
        }));
    }

    ReconcileDirective::EffectUnknown(json!({
        "error": format!("unsupported-reconciliation-action-type:{act_type}"),
    }))
}

fn check_action_freshness(
    action: &Value,
    fresh_risk_snapshot: &Value,
) -> Result<(), (bool, String)> {
    let act_type = action
        .get("type")
        .filter(|v| python_truthy(v))
        .map(|v| value_str(v).to_uppercase())
        .unwrap_or_default();
    let empty_vec = Vec::new();
    let risks = fresh_risk_snapshot
        .get("risks")
        .and_then(Value::as_array)
        .unwrap_or(&empty_vec);

    if act_type == "CREATE_REPAIR_JOB" {
        let pid = action_param_str(action, "policyId", &["policyId"]);
        let has_open = risks.iter().any(|r| {
            r.get("policyId")
                .filter(|v| python_truthy(v))
                .map(value_str)
                .unwrap_or_default()
                == pid
                && matches!(
                    r.get("type").and_then(Value::as_str),
                    Some("REPLICA_LAG" | "FAILURE_DOMAIN_VIOLATION")
                )
                && r.get("severity")
                    .filter(|v| python_truthy(v))
                    .map(|v| value_str(v).to_lowercase())
                    .is_some_and(|s| s != "healthy" && s != "low")
        });
        if !has_open {
            return Err((true, "replica-risk-already-cleared".to_string()));
        }
    } else if act_type == "CREATE_REBALANCE_JOB" {
        let src = action_param_str(action, "sourceTargetId", &["source", "target"]);
        let dst = action_param_str(action, "destTargetId", &["destination"]);
        let has_open = risks.iter().any(|r| {
            r.get("target")
                .filter(|v| python_truthy(v))
                .map(value_str)
                .unwrap_or_default()
                == src
                && r.get("type").and_then(Value::as_str) == Some("CAPACITY_EXHAUSTION")
                && r.get("severity")
                    .filter(|v| python_truthy(v))
                    .map(|v| value_str(v).to_lowercase())
                    .is_some_and(|s| s != "healthy" && s != "low")
        });
        if !has_open {
            return Err((true, "capacity-risk-already-cleared".to_string()));
        }
        let dst_target = registered_target(&dst).ok();
        let is_draining = dst_target.as_ref().is_none_or(|t| {
            t.get("status")
                .and_then(Value::as_str)
                .is_some_and(|s| s.eq_ignore_ascii_case("draining"))
        });
        if is_draining {
            return Err((
                false,
                "destination-target-draining-or-unavailable".to_string(),
            ));
        }
    } else if act_type == "START_DR_DRILL" {
        let dr_open = risks
            .iter()
            .find(|r| r.get("type").and_then(Value::as_str) == Some("DR_STALENESS"))
            .is_some_and(|r| {
                r.get("severity")
                    .filter(|v| python_truthy(v))
                    .map(|v| value_str(v).to_lowercase())
                    .is_some_and(|s| s != "healthy" && s != "low")
            });
        if !dr_open {
            return Err((true, "dr-staleness-already-cleared".to_string()));
        }
    }
    Ok(())
}

fn simulate_action_db(root: &Path, action: &Value) -> Result<(), String> {
    let act_type = action
        .get("type")
        .filter(|v| python_truthy(v))
        .map(|v| value_str(v).to_uppercase())
        .unwrap_or_default();

    if act_type == "CREATE_REBALANCE_JOB" {
        let src = action_param_str(action, "sourceTargetId", &["source", "target"]);
        let dst = action_param_str(action, "destTargetId", &["destination"]);
        if src.is_empty() || dst.is_empty() || src == dst {
            return Err("source-destination-identical-or-empty".to_string());
        }
        let dst_target = registered_target(&dst).ok();
        if dst_target.as_ref().is_none_or(|t| {
            t.get("status")
                .and_then(Value::as_str)
                .is_some_and(|s| s.eq_ignore_ascii_case("draining"))
        }) {
            return Err(format!("destination-target-invalid-or-draining:{dst}"));
        }
        let t_val = dst_target.unwrap_or_else(|| json!({}));
        let control_conn = crate::backup_catalog_routes::open_readonly_sqlite(
            &crate::resilience_state_support::control_db_path(root),
        )
        .ok()
        .flatten();
        let dr_conn = crate::backup_catalog_routes::open_readonly_sqlite(&dr_db_path(root))
            .ok()
            .flatten();
        if let Ok(cap) = probe_target_capacity_with_probe(
            &t_val,
            &dst,
            control_conn.as_ref(),
            dr_conn.as_ref(),
            false,
        ) {
            if let Some(free_pct) = cap.get("freePercent").and_then(Value::as_f64) {
                if free_pct <= 20.0 {
                    return Err(format!(
                        "destination-capacity-watermark-insufficient:{free_pct}%<=20%"
                    ));
                }
            }
        }
        return Ok(());
    }

    if act_type == "CREATE_REPAIR_JOB" {
        let pid = action_param_str(action, "policyId", &["policyId"]);
        let bid = action_param_str(action, "backupId", &["backupId"]);
        let dst = action_param_str(action, "destTargetId", &["destination", "target"]);
        if pid.is_empty() || bid.is_empty() {
            return Err("repair-policy-or-backup-missing".to_string());
        }
        let policies = list_policies_readonly(root).unwrap_or_default();
        if !policies
            .iter()
            .any(|p| p.get("policyId").and_then(Value::as_str) == Some(&pid))
        {
            return Err(format!("policy-not-found:{pid}"));
        }
        let dr_conn = crate::backup_catalog_routes::open_readonly_sqlite(&dr_db_path(root))
            .ok()
            .flatten();
        let has_pt = dr_conn.as_ref().is_some_and(|c| {
            crate::resilience_read_routes::table_exists(c, "recovery_points").unwrap_or(false)
                && c.query_row(
                    "SELECT 1 FROM recovery_points WHERE policy_id = ? AND backup_id = ? LIMIT 1",
                    rusqlite::params![pid, bid],
                    |r| r.get::<_, i64>(0),
                )
                .optional()
                .ok()
                .flatten()
                .is_some()
        });
        if !has_pt {
            return Err(format!("recovery-point-not-found:{pid}:{bid}"));
        }
        if !dst.is_empty() {
            let dst_target = registered_target(&dst).ok();
            if dst_target.as_ref().is_none_or(|t| {
                t.get("status")
                    .and_then(Value::as_str)
                    .is_some_and(|s| s.eq_ignore_ascii_case("draining"))
            }) {
                return Err(format!("repair-destination-target-invalid:{dst}"));
            }
        }
        return Ok(());
    }

    if act_type == "START_DR_DRILL" {
        return Ok(());
    }
    Err(format!("unsupported-simulation-type:{act_type}"))
}

fn find_receipt_and_commit(
    target_root: &Path,
    policy_id: &str,
    backup_id: &str,
    expected_osd: Option<&str>,
) -> (&'static str, Option<Value>, Option<Value>) {
    let rp = target_root
        .join("receipts")
        .join(format!("{backup_id}.json"));
    let mut cp = target_root
        .join("commits")
        .join(policy_id)
        .join(format!("{backup_id}.json"));

    let raw_receipt = if rp.is_file() {
        match fs::read(&rp) {
            Ok(b) => Some(b),
            Err(_) => return ("corrupt", None, None),
        }
    } else {
        None
    };
    let mut raw_commit = if cp.is_file() {
        match fs::read(&cp) {
            Ok(b) => Some(b),
            Err(_) => return ("corrupt", None, None),
        }
    } else {
        None
    };

    if raw_commit.is_none() {
        if let Some(ref r_bytes) = raw_receipt {
            if let Ok(r_val) = serde_json::from_slice::<Value>(r_bytes) {
                let slot = r_val
                    .get("scheduleSlot")
                    .and_then(Value::as_str)
                    .unwrap_or_default();
                if !slot.is_empty() {
                    let slot_hex = sha256_bytes(format!("{policy_id}:{slot}").as_bytes());
                    for name in [
                        format!("{slot_hex}.json"),
                        format!("{}.json", &slot_hex[..16.min(slot_hex.len())]),
                    ] {
                        let cand = target_root.join("commits").join(policy_id).join(name);
                        if cand.is_file() {
                            cp = cand;
                            raw_commit = fs::read(&cp).ok();
                            break;
                        }
                    }
                }
            }
            if raw_commit.is_none() {
                let c_dir = target_root.join("commits").join(policy_id);
                if c_dir.is_dir() {
                    if let Ok(entries) = fs::read_dir(&c_dir) {
                        for ent in entries.flatten() {
                            let path = ent.path();
                            if path.extension().and_then(|s| s.to_str()) == Some("json") {
                                if let Ok(c_bytes) = fs::read(&path) {
                                    if let Ok(c_val) = serde_json::from_slice::<Value>(&c_bytes) {
                                        if c_val.get("backupId").and_then(Value::as_str)
                                            == Some(backup_id)
                                        {
                                            raw_commit = Some(c_bytes);
                                            break;
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    if raw_receipt.is_none() && raw_commit.is_none() {
        return ("missing", None, None);
    }
    let (Some(r_bytes), Some(c_bytes)) = (raw_receipt, raw_commit) else {
        return ("corrupt", None, None);
    };
    let Ok(receipt) = serde_json::from_slice::<Value>(&r_bytes) else {
        return ("corrupt", None, None);
    };
    let Ok(commit) = serde_json::from_slice::<Value>(&c_bytes) else {
        return ("corrupt", None, None);
    };
    if !receipt.is_object() || !commit.is_object() {
        return ("corrupt", None, None);
    }
    let calc_receipt_digest = sha256_bytes(&r_bytes);
    if commit.get("receiptDigest").and_then(Value::as_str) != Some(calc_receipt_digest.as_str()) {
        return ("corrupt", Some(receipt), Some(commit));
    }
    let schema_ver = commit
        .get("schemaVersion")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    if !matches!(schema_ver, 1..=4) {
        return ("corrupt", Some(receipt), Some(commit));
    }
    if commit.get("policyId").and_then(Value::as_str) != Some(policy_id)
        || commit.get("backupId").and_then(Value::as_str) != Some(backup_id)
        || receipt.get("policyId").and_then(Value::as_str) != Some(policy_id)
        || receipt.get("backupId").and_then(Value::as_str) != Some(backup_id)
    {
        return ("conflicting", Some(receipt), Some(commit));
    }
    let r_osd = receipt
        .get("objectSetDigest")
        .or_else(|| receipt.get("objectDigest"))
        .filter(|v| python_truthy(v))
        .map(value_str);
    let Some(r_osd_str) = r_osd else {
        return ("corrupt", Some(receipt), Some(commit));
    };
    if let Some(c_osd) = commit
        .get("objectSetDigest")
        .or_else(|| commit.get("objectDigest"))
        .filter(|v| python_truthy(v))
        .map(value_str)
    {
        if c_osd != r_osd_str {
            return ("corrupt", Some(receipt), Some(commit));
        }
    }
    if let Some(exp) = expected_osd.filter(|s| !s.is_empty()) {
        if r_osd_str != exp {
            return ("conflicting", Some(receipt), Some(commit));
        }
    }
    if commit.get("commitHash").is_some_and(python_truthy) {
        let calc = commit_hash(&commit);
        if commit.get("commitHash").and_then(Value::as_str) != Some(calc.as_str()) {
            return ("corrupt", Some(receipt), Some(commit));
        }
    }
    ("authenticated", Some(receipt), Some(commit))
}

fn resolve_component_rel_path(source_root: &Path, receipt: &Value, digest: &str) -> String {
    let comp_rel = format!("objects/{}/{}/{digest}.age", &digest[..2], &digest[2..4]);
    let control_rel = format!("control/{digest}.age");
    let sha256_rel = format!("objects/sha256/{}/{digest}.age", &digest[..2]);
    let fn_rel = receipt
        .get("filename")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    if source_root.join(&comp_rel).is_file() {
        return comp_rel;
    }
    if source_root.join(&control_rel).is_file() {
        return control_rel;
    }
    if source_root.join(&sha256_rel).is_file() {
        return sha256_rel;
    }
    if !fn_rel.is_empty() && source_root.join(&fn_rel).is_file() {
        return fn_rel;
    }
    comp_rel
}

fn sha256_file_streaming(path: &Path) -> Result<(String, u64), AppError> {
    let mut f = File::open(path).map_err(|_| unhandled_error())?;
    let mut hasher = Sha256::new();
    let mut buf = vec![0u8; CHUNK_SIZE];
    let mut total = 0_u64;
    loop {
        let n = f.read(&mut buf).map_err(|_| unhandled_error())?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
        total += n as u64;
    }
    Ok((format!("{:x}", hasher.finalize()), total))
}

fn copy_verified_ciphertext(
    src_file: &Path,
    dst_file: &Path,
    expected_digest: &str,
) -> Result<u64, AppError> {
    if !src_file.is_file() {
        return Err(not_found_error(format!(
            "Source ciphertext object missing: {}",
            src_file.display()
        )));
    }
    if let Some(parent) = dst_file.parent() {
        fs::create_dir_all(parent).map_err(|_| unhandled_error())?;
    }
    let tmp_name = format!(
        ".{}.{}.tmp",
        dst_file
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("object"),
        &new_hex_id()[..8]
    );
    let tmp_path = dst_file.with_file_name(tmp_name);
    let mut reader = File::open(src_file).map_err(|_| unhandled_error())?;
    let mut writer = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&tmp_path)
        .map_err(|_| unhandled_error())?;
    let mut hasher = Sha256::new();
    let mut buf = vec![0u8; CHUNK_SIZE];
    let mut total = 0_u64;
    let copy_res: Result<(), AppError> = (|| {
        loop {
            let n = reader.read(&mut buf).map_err(|_| unhandled_error())?;
            if n == 0 {
                break;
            }
            hasher.update(&buf[..n]);
            writer.write_all(&buf[..n]).map_err(|_| unhandled_error())?;
            total += n as u64;
        }
        writer.flush().map_err(|_| unhandled_error())?;
        writer.sync_all().map_err(|_| unhandled_error())?;
        Ok(())
    })();
    drop(writer);
    if let Err(err) = copy_res {
        let _ = fs::remove_file(&tmp_path);
        return Err(err);
    }
    let calc_digest = format!("{:x}", hasher.finalize());
    if calc_digest != expected_digest {
        let _ = fs::remove_file(&tmp_path);
        return Err(AppError {
            status: 500,
            code: codes::INTERNAL,
            message: format!(
                "source component corrupt: ciphertext transfer digest mismatch: calculated={calc_digest}, expected={expected_digest}"
            ),
        });
    }
    fs::rename(&tmp_path, dst_file).map_err(|_| {
        let _ = fs::remove_file(&tmp_path);
        unhandled_error()
    })?;
    Ok(total)
}

fn write_repair_job_fs(root: &Path, job: &Value) -> Result<(), AppError> {
    let dir = root.join(".backup-replication").join("repairs");
    fs::create_dir_all(&dir).map_err(|_| unhandled_error())?;
    let r_id = job
        .get("repairId")
        .and_then(Value::as_str)
        .unwrap_or("unknown");
    write_atomic_json(&dir.join(format!("{r_id}.json")), job)
}

fn write_rebalance_job_fs(root: &Path, job: &Value) -> Result<(), AppError> {
    let dir = root.join(".backup-rebalance");
    fs::create_dir_all(&dir).map_err(|_| unhandled_error())?;
    let j_id = job
        .get("jobId")
        .and_then(Value::as_str)
        .unwrap_or("unknown");
    write_atomic_json(&dir.join(format!("{j_id}.json")), job)
}

fn record_repaired_copy_ledger(
    target_id: &str,
    policy_id: &str,
    backup_id: &str,
    committed_at: &str,
    object_set_digest: Option<&str>,
    bytes_transferred: i64,
    duration_ms: i64,
) -> Result<(), AppError> {
    let (_, now_iso) = now_unix_secs_and_iso();
    let osd_str = object_set_digest.unwrap_or("");
    let prefix = if osd_str.len() >= 16 {
        &osd_str[..16]
    } else if osd_str.is_empty() {
        "na"
    } else {
        osd_str
    };
    let logical_id = format!("lrp_{policy_id}_{backup_id}_{prefix}");
    let conn = open_dr_ledger_write()?;
    conn.execute(
        "INSERT INTO logical_recovery_points (
            logical_id, policy_id, backup_id, object_set_digest, committed_at, snapshot_kind, retained, metadata_json
         ) VALUES (?, ?, ?, ?, ?, 'full', 1, NULL)
         ON CONFLICT(logical_id) DO UPDATE SET
            committed_at = CASE
                WHEN excluded.committed_at > logical_recovery_points.committed_at
                THEN excluded.committed_at ELSE logical_recovery_points.committed_at END",
        rusqlite::params![
            logical_id,
            policy_id,
            backup_id,
            object_set_digest.filter(|s| !s.is_empty()),
            committed_at,
        ],
    )
    .map_err(|_| unhandled_error())?;
    conn.execute(
        "INSERT INTO recovery_point_copies (
            target_id, policy_id, backup_id, logical_id, object_set_digest, committed_at,
            recoverable, role, mode, state, last_verified_at, last_repair_at, verified_at
         ) VALUES (?, ?, ?, ?, ?, ?, 1, 'replica', 'required', 'healthy', ?, ?, ?)
         ON CONFLICT(target_id, policy_id, backup_id) DO UPDATE SET
            logical_id = excluded.logical_id,
            object_set_digest = excluded.object_set_digest,
            committed_at = excluded.committed_at,
            recoverable = 1,
            role = 'replica',
            mode = 'required',
            state = 'healthy',
            last_verified_at = excluded.last_verified_at,
            last_repair_at = excluded.last_repair_at,
            verified_at = excluded.verified_at",
        rusqlite::params![
            target_id,
            policy_id,
            backup_id,
            logical_id,
            object_set_digest.filter(|s| !s.is_empty()),
            committed_at,
            now_iso,
            now_iso,
            now_iso,
        ],
    )
    .map_err(|_| unhandled_error())?;
    drop(conn);

    record_stage_sample(&StageSampleRecord {
        sample_id: &format!("stage_{}", &new_hex_id()[..16]),
        target_id: Some(target_id),
        recovery_class: &Value::Null,
        stage: "repair",
        bytes: bytes_transferred,
        duration_ms,
        observed_at: &now_iso,
        result: "success",
    })?;
    Ok(())
}

fn create_repair_job_fs(
    root: &Path,
    policy_id: &str,
    backup_id: &str,
    source_target_id: Option<&str>,
    dest_target_id: &str,
    resilience_action_id: Option<&str>,
) -> Result<Value, AppError> {
    if let Some(aid) = resilience_action_id {
        for j in read_json_dir_records(&root.join(".backup-replication").join("repairs")) {
            if j.get("policyId").and_then(Value::as_str) == Some(policy_id)
                && j.get("backupId").and_then(Value::as_str) == Some(backup_id)
                && j.get("resilienceActionId").and_then(Value::as_str) == Some(aid)
            {
                return Ok(j);
            }
        }
    }
    let (_, now_iso) = now_unix_secs_and_iso();
    let r_id = format!("repair_{}", &new_hex_id()[..16]);
    let job = json!({
        "schemaVersion": 2,
        "repairId": r_id,
        "resilienceActionId": resilience_action_id,
        "policyId": policy_id,
        "backupId": backup_id,
        "sourceTargetId": source_target_id.filter(|s| !s.is_empty()),
        "destTargetId": dest_target_id,
        "objectSetDigest": Value::Null,
        "repairMode": "auto",
        "trafficClass": 2,
        "phase": "queued",
        "components": {},
        "bytesRepaired": 0,
        "attempt": 0,
        "maxAttempts": 5,
        "nextAttemptAt": Value::Null,
        "holdId": Value::Null,
        "createdAt": now_iso,
        "updatedAt": now_iso,
        "error": Value::Null,
    });
    write_repair_job_fs(root, &job)?;
    Ok(job)
}

fn execute_replica_repair_native(
    root: &Path,
    repair_id: &str,
    requested_source: Option<&str>,
    instance_id: &str,
) -> Result<Value, AppError> {
    let started = Instant::now();
    let mut job = read_repair_job_fs(root, repair_id)
        .ok_or_else(|| not_found_error("ReplicaRepairJob not found"))?;
    let cur_phase = job
        .get("phase")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    if matches!(
        cur_phase.as_str(),
        "healthy"
            | "failed-terminal"
            | "failed"
            | "quarantined"
            | "superseded"
            | "skipped"
            | "cancelled"
    ) {
        return Ok(json!({
            "status": if cur_phase == "healthy" { "success" } else { cur_phase.as_str() },
            "repairId": repair_id,
            "job": job,
        }));
    }

    let attempt = job.get("attempt").and_then(Value::as_i64).unwrap_or(0) + 1;
    let max_attempts = job.get("maxAttempts").and_then(Value::as_i64).unwrap_or(5);
    if attempt > max_attempts {
        job["phase"] = Value::String("failed-terminal".to_string());
        job["error"] = Value::String("max-attempts-exceeded".to_string());
        job["attempt"] = json!(attempt);
        job["updatedAt"] = Value::String(now_unix_secs_and_iso().1);
        write_repair_job_fs(root, &job)?;
        return Err(conflict_error("ReplicaRepairJob max attempts exceeded"));
    }

    let policy_id = job
        .get("policyId")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let backup_id = job
        .get("backupId")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let dest_target_id = job
        .get("destTargetId")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let mut source_target_id = job
        .get("sourceTargetId")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .or_else(|| {
            requested_source
                .filter(|s| !s.is_empty())
                .map(str::to_string)
        })
        .unwrap_or_default();

    let set_phase = |j: &mut Value, ph: &str| {
        j["phase"] = Value::String(ph.to_string());
        j["updatedAt"] = Value::String(now_unix_secs_and_iso().1);
    };
    job["attempt"] = json!(attempt);
    set_phase(&mut job, "selecting-source");
    write_repair_job_fs(root, &job)?;

    if source_target_id.is_empty() {
        let dr_conn = crate::backup_catalog_routes::open_readonly_sqlite(&dr_db_path(root))?;
        let copies = list_logical_recovery_copies(
            dr_conn.as_ref(),
            Some(&policy_id),
            Some(&backup_id),
            None,
            1000,
        )?;
        if let Some(found) = copies.iter().find(|c| {
            c.get("targetId").and_then(Value::as_str) != Some(&dest_target_id)
                && c.get("recoverable").is_some_and(python_truthy)
                && matches!(
                    c.get("state").and_then(Value::as_str),
                    Some("healthy" | "committed" | "active")
                )
        }) {
            source_target_id = found
                .get("targetId")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
        } else if let Some(ref conn) = dr_conn {
            if crate::resilience_read_routes::table_exists(conn, "recovery_points")? {
                if let Some(tid) = conn
                    .query_row(
                        "SELECT target_id FROM recovery_points
                         WHERE policy_id = ? AND backup_id = ? AND target_id != ? AND recoverable != 0
                         ORDER BY committed_at DESC LIMIT 1",
                        rusqlite::params![policy_id, backup_id, dest_target_id],
                        |r| r.get::<_, String>(0),
                    )
                    .optional()
                    .map_err(|_| unhandled_error())?
                {
                    source_target_id = tid;
                }
            }
        }
        if source_target_id.is_empty() {
            set_phase(&mut job, "failed-terminal");
            job["error"] = Value::String("no-healthy-source-copy".to_string());
            write_repair_job_fs(root, &job)?;
            return Err(not_found_error(format!(
                "No healthy source copy available to repair {dest_target_id} for {backup_id}"
            )));
        }
        job["sourceTargetId"] = Value::String(source_target_id.clone());
        write_repair_job_fs(root, &job)?;
    }

    let source_root = resolve_target_root(&source_target_id)?;
    let dest_root = resolve_target_root(&dest_target_id)?;

    set_phase(&mut job, "acquiring-source-hold");
    write_repair_job_fs(root, &job)?;
    let (now_secs, now_iso) = now_unix_secs_and_iso();
    let hold_id = format!(
        "hold_{source_target_id}_{policy_id}_{backup_id}_{}",
        &new_hex_id()[..8]
    );
    let hold_payload = json!({
        "holderKind": "replica-repair",
        "holderId": repair_id,
        "holdId": hold_id,
        "targetId": source_target_id,
        "policyId": policy_id,
        "backupId": backup_id,
        "objectSetDigest": job.get("objectSetDigest").cloned().unwrap_or(Value::Null),
        "createdAt": now_iso,
        "expiresAt": utc_iso_from_unix(now_secs + 3600),
        "generation": 1,
    });
    let j_hold = root
        .join(".backup-replication")
        .join("holds")
        .join(format!("{hold_id}.json"));
    let t_hold = source_root
        .join("holds")
        .join("repair")
        .join(format!("{hold_id}.json"));
    write_atomic_json(&j_hold, &hold_payload)?;
    write_atomic_json(&t_hold, &hold_payload)?;
    let _hold_guard = SourceHoldGuard {
        journal_hold: j_hold,
        target_hold: t_hold,
    };

    job["holdId"] = Value::String(hold_id);
    set_phase(&mut job, "validating-source-control");
    write_repair_job_fs(root, &job)?;

    let (s_status, s_receipt_opt, _) = find_receipt_and_commit(
        &source_root,
        &policy_id,
        &backup_id,
        job.get("objectSetDigest").and_then(Value::as_str),
    );
    let Some(source_receipt) = s_receipt_opt.filter(|_| s_status == "authenticated") else {
        set_phase(&mut job, "failed-terminal");
        job["error"] = Value::String(format!("source-control-plane-{s_status}"));
        write_repair_job_fs(root, &job)?;
        if s_status == "missing" {
            return Err(not_found_error(format!(
                "Source receipt missing on target {source_target_id}; cannot repair"
            )));
        }
        return Err(conflict_error(format!(
            "Source control plane {s_status}; rejected"
        )));
    };

    let mut lease = acquire_target_writer(&dest_root, &dest_target_id)?;
    let fencing = lease.fencing_token();
    lease.renew()?;
    let _writer_guard = TargetWriterGuard { lease };

    let mut objects = source_receipt
        .get("objects")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if objects.is_empty() {
        if let Some(dig) = source_receipt
            .get("objectDigest")
            .filter(|v| python_truthy(v))
            .map(value_str)
        {
            objects.push(json!({
                "digest": dig,
                "size": source_receipt.get("size").and_then(Value::as_i64).unwrap_or(0),
            }));
        }
    }

    set_phase(&mut job, "scanning-destination");
    write_repair_job_fs(root, &job)?;
    let expected_osd = source_receipt
        .get("objectSetDigest")
        .or_else(|| source_receipt.get("objectDigest"))
        .filter(|v| python_truthy(v))
        .map(value_str);
    let (d_status, _, _) =
        find_receipt_and_commit(&dest_root, &policy_id, &backup_id, expected_osd.as_deref());
    let repair_mode = if d_status == "authenticated" {
        "heal-existing"
    } else if d_status == "missing" {
        "provision"
    } else {
        set_phase(&mut job, "quarantined");
        job["error"] = Value::String(format!("destination-control-plane-{d_status}"));
        write_repair_job_fs(root, &job)?;
        return Err(conflict_error(format!(
            "Destination control plane {d_status}; copy quarantined and cannot heal"
        )));
    };

    job["repairMode"] = Value::String(repair_mode.to_string());
    let mut comp_map = job
        .get("components")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    for obj in &objects {
        let dig = obj
            .get("digest")
            .and_then(Value::as_str)
            .unwrap_or_default();
        if dig.is_empty() || comp_map.contains_key(dig) {
            continue;
        }
        comp_map.insert(
            dig.to_string(),
            json!({
                "digest": dig,
                "size": obj.get("size").and_then(Value::as_i64).unwrap_or(0),
                "state": "pending",
                "transferredBytes": 0,
            }),
        );
    }
    job["components"] = Value::Object(comp_map.clone());
    set_phase(&mut job, "transferring-components");
    write_repair_job_fs(root, &job)?;

    let mut bytes_transferred = job
        .get("bytesRepaired")
        .and_then(Value::as_u64)
        .unwrap_or(0);
    for obj in &objects {
        let dig = obj
            .get("digest")
            .and_then(Value::as_str)
            .unwrap_or_default();
        if dig.len() < 4 {
            continue;
        }
        if comp_map
            .get(dig)
            .and_then(|c| c.get("state"))
            .and_then(Value::as_str)
            == Some("verified")
        {
            continue;
        }
        let rel = resolve_component_rel_path(&source_root, &source_receipt, dig);
        let src_path = source_root.join(&rel);
        let dst_path = dest_root.join(&rel);
        let mut need_copy = true;
        if dst_path.is_file() {
            if let Ok((existing_dig, _)) = sha256_file_streaming(&dst_path) {
                if existing_dig == dig {
                    need_copy = false;
                } else {
                    let q_path = dest_root.join(".quarantine").join(&rel);
                    if let Some(parent) = q_path.parent() {
                        let _ = fs::create_dir_all(parent);
                    }
                    let _ = fs::rename(&dst_path, &q_path);
                }
            }
        }
        let copied = if need_copy {
            copy_verified_ciphertext(&src_path, &dst_path, dig)?
        } else {
            0
        };
        bytes_transferred += copied;
        comp_map.insert(
            dig.to_string(),
            json!({
                "digest": dig,
                "size": obj.get("size").and_then(Value::as_i64).unwrap_or(0),
                "state": "verified",
                "transferredBytes": copied,
            }),
        );
        job["components"] = Value::Object(comp_map.clone());
        job["bytesRepaired"] = json!(bytes_transferred);
        write_repair_job_fs(root, &job)?;
    }

    set_phase(&mut job, "verifying-components");
    write_repair_job_fs(root, &job)?;
    set_phase(&mut job, "finalizing");
    write_repair_job_fs(root, &job)?;

    let commit_ts = now_unix_secs_and_iso().1;
    if repair_mode == "provision" {
        let mut dest_receipt = source_receipt.clone();
        dest_receipt["targetId"] = Value::String(dest_target_id.clone());
        let dest_receipt_str =
            serde_json::to_string_pretty(&dest_receipt).map_err(|_| unhandled_error())? + "\n";
        let dest_receipt_bytes = dest_receipt_str.as_bytes();
        let dest_receipt_digest = sha256_bytes(dest_receipt_bytes);
        let rp = dest_root.join("receipts").join(format!("{backup_id}.json"));
        if let Some(parent) = rp.parent() {
            fs::create_dir_all(parent).map_err(|_| unhandled_error())?;
        }
        if !rp.exists() {
            fs::write(&rp, dest_receipt_bytes).map_err(|_| unhandled_error())?;
        }

        let prev_marker = latest_commit(&dest_root);
        let next_gen = prev_marker
            .as_ref()
            .map(|m| m.generation() + 1)
            .unwrap_or(1);
        let prev_hash = prev_marker
            .as_ref()
            .map(|m| m.commit_hash())
            .unwrap_or_else(|| GENESIS_COMMIT_HASH.to_string());
        let slot = format!("repair/{backup_id}");
        let mut commit = json!({
            "schemaVersion": 4,
            "targetGeneration": next_gen,
            "previousCommitHash": prev_hash,
            "fencingToken": fencing,
            "runId": repair_id,
            "policyId": policy_id,
            "scheduleSlot": slot,
            "slotDigest": sha256_bytes(format!("{policy_id}:{slot}").as_bytes()),
            "backupId": backup_id,
            "committedAt": commit_ts,
            "receiptDigest": dest_receipt_digest,
            "storageProtocol": if dest_receipt.get("objectSetDigest").is_some_and(python_truthy) {
                "object-set-v1"
            } else {
                "whole-age-v1"
            },
            "objectSetDigest": dest_receipt.get("objectSetDigest").cloned().unwrap_or(Value::Null),
            "controlObjectDigest": dest_receipt.get("controlObjectDigest").cloned().unwrap_or(Value::Null),
        });
        let c_hash = commit_hash(&commit);
        commit["commitHash"] = Value::String(c_hash.clone());
        let cp = dest_root
            .join("commits")
            .join(&policy_id)
            .join(format!("{backup_id}.json"));
        write_atomic_json(&cp, &commit)?;
        let head = json!({"latestCommitHash": c_hash, "targetGeneration": next_gen});
        write_atomic_json(&dest_root.join("control").join("head.json"), &head)?;

        let cat_dir = dest_root.join("catalogs");
        fs::create_dir_all(&cat_dir).map_err(|_| unhandled_error())?;
        let mut cat_file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(cat_dir.join(format!("{policy_id}.jsonl")))
            .map_err(|_| unhandled_error())?;
        writeln!(cat_file, "{}", dumps_compact(&dest_receipt)).map_err(|_| unhandled_error())?;
    }

    let elapsed_ms = started.elapsed().as_millis().max(1) as i64;
    record_repaired_copy_ledger(
        &dest_target_id,
        &policy_id,
        &backup_id,
        &commit_ts,
        expected_osd.as_deref(),
        bytes_transferred as i64,
        elapsed_ms,
    )?;

    job["bytesRepaired"] = json!(bytes_transferred);
    job["durationMs"] = json!(elapsed_ms as f64);
    job["error"] = Value::Null;
    job["instanceId"] = Value::String(instance_id.to_string());
    set_phase(&mut job, "healthy");
    write_repair_job_fs(root, &job)?;

    Ok(json!({
        "status": "success",
        "repairId": repair_id,
        "policyId": policy_id,
        "backupId": backup_id,
        "sourceTargetId": source_target_id,
        "destTargetId": dest_target_id,
        "repairMode": repair_mode,
        "bytesRepaired": bytes_transferred,
        "durationMs": elapsed_ms as f64,
        "phase": "healthy",
        "job": job,
    }))
}

fn create_rebalance_job_fs(
    root: &Path,
    policy_id: &str,
    backup_id: &str,
    source_target_id: &str,
    dest_target_id: &str,
    reason: &str,
    resilience_action_id: Option<&str>,
) -> Result<Value, AppError> {
    let all = read_json_dir_records(&root.join(".backup-rebalance"));
    for j in &all {
        if j.get("policyId").and_then(Value::as_str) == Some(policy_id)
            && j.get("backupId").and_then(Value::as_str) == Some(backup_id)
            && j.get("destTargetId").and_then(Value::as_str) == Some(dest_target_id)
        {
            if resilience_action_id.is_some()
                && j.get("resilienceActionId").and_then(Value::as_str) == resilience_action_id
            {
                return Ok(j.clone());
            }
            if !matches!(
                j.get("phase").and_then(Value::as_str),
                Some("complete" | "failed" | "cancelled")
            ) {
                return Ok(j.clone());
            }
        }
    }
    let (_, now_iso) = now_unix_secs_and_iso();
    let job_id = format!("rebalance_{}", &new_hex_id()[..16]);
    let body = json!({
        "schemaVersion": 1,
        "jobId": job_id,
        "resilienceActionId": resilience_action_id,
        "policyId": policy_id,
        "backupId": backup_id,
        "sourceTargetId": source_target_id,
        "destTargetId": dest_target_id,
        "reason": reason,
        "pruneSourceAfter": false,
        "phase": "pending",
        "bytesTransferred": 0,
        "createdAt": now_iso,
        "updatedAt": now_iso,
    });
    write_rebalance_job_fs(root, &body)?;
    Ok(body)
}

fn execute_rebalance_job_native(
    root: &Path,
    job_id: &str,
    instance_id: &str,
) -> Result<Value, AppError> {
    let mut job = read_rebalance_job_fs(root, job_id)
        .ok_or_else(|| not_found_error("rebalance job not found"))?;
    let phase = job.get("phase").and_then(Value::as_str).unwrap_or_default();
    if phase == "cancelled" {
        return Ok(json!({"status": "cancelled", "jobId": job_id, "job": job}));
    }
    if phase == "complete" {
        return Ok(json!({"status": "success", "jobId": job_id, "job": job}));
    }
    let set_phase = |j: &mut Value, ph: &str| {
        j["phase"] = Value::String(ph.to_string());
        j["updatedAt"] = Value::String(now_unix_secs_and_iso().1);
    };
    set_phase(&mut job, "transferring");
    write_rebalance_job_fs(root, &job)?;

    let policy_id = job
        .get("policyId")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let backup_id = job
        .get("backupId")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let source_target_id = job
        .get("sourceTargetId")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let dest_target_id = job
        .get("destTargetId")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let action_id_opt = job
        .get("resilienceActionId")
        .and_then(Value::as_str)
        .map(str::to_string);

    let sub_repair = create_repair_job_fs(
        root,
        &policy_id,
        &backup_id,
        Some(&source_target_id),
        &dest_target_id,
        action_id_opt.as_deref(),
    )?;
    let sub_rid = sub_repair
        .get("repairId")
        .and_then(Value::as_str)
        .unwrap_or_default();
    match execute_replica_repair_native(root, sub_rid, Some(&source_target_id), instance_id) {
        Ok(rep_res) => {
            if rep_res.get("status").and_then(Value::as_str) != Some("success") {
                set_phase(&mut job, "failed");
                job["error"] = Value::String("rebalance-transfer-failed".to_string());
                let _ = write_rebalance_job_fs(root, &job);
                return Ok(json!({"status": "failed", "jobId": job_id, "job": job}));
            }
            let bytes = rep_res
                .get("bytesRepaired")
                .and_then(Value::as_i64)
                .unwrap_or(0);
            job["bytesTransferred"] = json!(bytes);
            job["durationMs"] = rep_res
                .get("durationMs")
                .cloned()
                .unwrap_or_else(|| json!(1.0));
            set_phase(&mut job, "verifying");
            write_rebalance_job_fs(root, &job)?;

            let dest_root = resolve_target_root(&dest_target_id)?;
            let (d_st, _, _) = find_receipt_and_commit(&dest_root, &policy_id, &backup_id, None);
            if d_st != "authenticated" {
                set_phase(&mut job, "failed");
                job["error"] = Value::String(format!(
                    "Rebalance destination copy failed authentication: {d_st}"
                ));
                write_rebalance_job_fs(root, &job)?;
                return Err(conflict_error(format!(
                    "Rebalance destination copy failed authentication: {d_st}"
                )));
            }
            set_phase(&mut job, "committed");
            write_rebalance_job_fs(root, &job)?;
            set_phase(&mut job, "complete");
            write_rebalance_job_fs(root, &job)?;
            Ok(json!({"status": "success", "jobId": job_id, "job": job}))
        }
        Err(err) => {
            set_phase(&mut job, "failed");
            job["error"] = Value::String(err.message.clone());
            let _ = write_rebalance_job_fs(root, &job);
            Ok(json!({"status": "failed", "jobId": job_id, "error": err.message, "job": job}))
        }
    }
}

fn verify_action_outcome_db(
    root: &Path,
    action: &Value,
    execution_result: &Value,
) -> (bool, Value) {
    let act_type = action
        .get("type")
        .filter(|v| python_truthy(v))
        .map(|v| value_str(v).to_uppercase())
        .unwrap_or_default();
    let (_, now_iso) = now_unix_secs_and_iso();

    if act_type == "CREATE_REPAIR_JOB" {
        let repair_job = execution_result.get("job").filter(|v| v.is_object());
        let failed = repair_job.is_some_and(|j| {
            j.get("status").and_then(Value::as_str) == Some("failed")
                || matches!(
                    j.get("phase").and_then(Value::as_str),
                    Some("failed" | "failed-terminal")
                )
        }) || execution_result.get("status").and_then(Value::as_str) == Some("failed");
        if failed {
            let err = repair_job
                .and_then(|j| j.get("error"))
                .or_else(|| execution_result.get("error"))
                .filter(|v| python_truthy(v))
                .map(value_str)
                .unwrap_or_else(|| "repair-job-failed".to_string());
            return (false, json!({"executionVerified": false, "error": err}));
        }

        let pid = action_param_str(action, "policyId", &["policyId"]);
        let bid = action_param_str(action, "backupId", &["backupId"]);
        let dst = action_param_str(action, "destTargetId", &["destination", "target"]);
        let dr_conn = crate::backup_catalog_routes::open_readonly_sqlite(&dr_db_path(root))
            .ok()
            .flatten();
        let copies =
            list_logical_recovery_copies(dr_conn.as_ref(), Some(&pid), Some(&bid), None, 1000)
                .unwrap_or_default();
        let committed: Vec<&Value> = copies
            .iter()
            .filter(|c| {
                matches!(
                    c.get("status")
                        .or_else(|| c.get("state"))
                        .filter(|v| python_truthy(v))
                        .map(|v| value_str(v).to_lowercase())
                        .as_deref(),
                    Some("committed" | "active" | "healthy")
                )
            })
            .collect();
        let policies = list_policies_readonly(root).unwrap_or_default();
        let pol = policies
            .iter()
            .find(|p| p.get("policyId").and_then(Value::as_str) == Some(&pid));
        let min_copies = pol
            .and_then(|p| p.get("replication"))
            .and_then(|r| r.get("minCommittedCopies"))
            .and_then(Value::as_i64)
            .filter(|v| *v != 0)
            .unwrap_or(1) as usize;
        let min_fds = pol
            .and_then(|p| p.get("replication"))
            .and_then(|r| r.get("minFailureDomains"))
            .and_then(Value::as_i64)
            .filter(|v| *v != 0)
            .unwrap_or(1) as usize;
        if committed.len() < min_copies {
            return (
                false,
                json!({
                    "executionVerified": false,
                    "error": format!("committed-copies-insufficient:{}<{min_copies}", committed.len()),
                    "committedCopies": committed.len(),
                }),
            );
        }
        if !dst.is_empty() {
            match resolve_target_root(&dst) {
                Ok(dst_root) => {
                    let (a_st, r_opt, c_opt) = find_receipt_and_commit(&dst_root, &pid, &bid, None);
                    if a_st != "authenticated" || r_opt.is_none() || c_opt.is_none() {
                        return (
                            false,
                            json!({
                                "executionVerified": false,
                                "error": format!("repair-destination-authentication-failed:{a_st}"),
                            }),
                        );
                    }
                }
                Err(e) => {
                    return (
                        false,
                        json!({
                            "executionVerified": false,
                            "error": format!("destination-target-auth-error:{}", e.message),
                        }),
                    );
                }
            }
        }
        let targets: HashMap<String, Value> = list_registered_targets()
            .unwrap_or_default()
            .into_iter()
            .filter_map(|t| {
                let tid = t.get("targetId").and_then(Value::as_str)?.to_string();
                Some((tid, t))
            })
            .collect();
        let mut domains = BTreeSet::new();
        for c in &committed {
            let tid = c
                .get("targetId")
                .filter(|v| python_truthy(v))
                .map(value_str)
                .unwrap_or_default();
            let fd = targets
                .get(&tid)
                .and_then(|t| t.get("failureDomain"))
                .filter(|v| python_truthy(v))
                .map(value_str)
                .unwrap_or_else(|| {
                    if tid.is_empty() {
                        "default".to_string()
                    } else {
                        tid
                    }
                });
            domains.insert(fd);
        }
        if domains.len() < min_fds {
            let dom_vec: Vec<String> = domains.into_iter().collect();
            return (
                false,
                json!({
                    "executionVerified": false,
                    "error": format!("failure-domain-objective-unsatisfied:{}<{min_fds}", dom_vec.len()),
                    "failureDomains": dom_vec,
                }),
            );
        }
        let dom_vec: Vec<String> = domains.into_iter().collect();
        return (
            true,
            json!({
                "executionVerified": true,
                "committedCopies": committed.len(),
                "destinationTarget": dst,
                "failureDomains": dom_vec,
                "verifiedAt": now_iso,
            }),
        );
    }

    if act_type == "CREATE_REBALANCE_JOB" {
        let job = execution_result.get("job").filter(|v| v.is_object());
        let failed = job.is_some_and(|j| {
            j.get("status").and_then(Value::as_str) == Some("failed")
                || matches!(
                    j.get("phase").and_then(Value::as_str),
                    Some("failed" | "failed-terminal")
                )
        }) || execution_result.get("status").and_then(Value::as_str) == Some("failed");
        if failed {
            let err = job
                .and_then(|j| j.get("error"))
                .or_else(|| execution_result.get("error"))
                .filter(|v| python_truthy(v))
                .map(value_str)
                .unwrap_or_else(|| "rebalance-job-failed".to_string());
            return (false, json!({"executionVerified": false, "error": err}));
        }
        let job_id = job
            .and_then(|j| j.get("jobId"))
            .or_else(|| execution_result.get("jobId"))
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        if job_id.is_empty() {
            return (
                false,
                json!({"executionVerified": false, "error": "rebalance-job-id-missing"}),
            );
        }
        let src = action_param_str(action, "sourceTargetId", &["source", "target"]);
        let dst = action_param_str(action, "destTargetId", &["destination"]);
        let pid = action_param_str(action, "policyId", &["policyId"]);
        let bid = action_param_str(action, "backupId", &["backupId"]);
        if !dst.is_empty() && !pid.is_empty() && !bid.is_empty() {
            match resolve_target_root(&dst) {
                Ok(dst_root) => {
                    let (a_st, r_opt, c_opt) = find_receipt_and_commit(&dst_root, &pid, &bid, None);
                    if a_st != "authenticated" || r_opt.is_none() || c_opt.is_none() {
                        return (
                            false,
                            json!({
                                "executionVerified": false,
                                "error": format!("rebalance-destination-not-authenticated:{a_st}"),
                            }),
                        );
                    }
                }
                Err(e) => {
                    return (
                        false,
                        json!({
                            "executionVerified": false,
                            "error": format!("rebalance-destination-auth-error:{}", e.message),
                        }),
                    );
                }
            }
        }
        return (
            true,
            json!({
                "executionVerified": true,
                "rebalanceJobId": job_id,
                "sourceTargetId": src,
                "destTargetId": dst,
                "verifiedAt": now_iso,
            }),
        );
    }

    if act_type == "START_DR_DRILL" {
        let success = execution_result.get("success") == Some(&Value::Bool(true))
            || execution_result
                .get("status")
                .filter(|v| python_truthy(v))
                .is_some_and(|v| {
                    matches!(value_str(v).to_lowercase().as_str(), "pass" | "success")
                });
        if !success {
            let err = execution_result
                .get("error")
                .filter(|v| python_truthy(v))
                .map(value_str)
                .unwrap_or_else(|| "dr-drill-failed".to_string());
            return (false, json!({"executionVerified": false, "error": err}));
        }
        let Some(proof) = execution_result
            .get("proof")
            .and_then(Value::as_object)
            .filter(|p| !p.is_empty())
        else {
            return (
                false,
                json!({"executionVerified": false, "error": "dr-drill-proof-required"}),
            );
        };
        let mut proof_errors = Vec::new();
        for req_f in [
            "schema",
            "drillId",
            "backupId",
            "testedBackupId",
            "restoreDurationMs",
            "workspaceDigestBefore",
            "workspaceDigestAfter",
            "objectCount",
            "commitVerified",
            "receiptVerified",
            "ageVerified",
            "cleanupCompleted",
        ] {
            if proof.get(req_f).is_none_or(Value::is_null) {
                proof_errors.push(format!("missing-{req_f}"));
            }
        }
        let schema_str = proof
            .get("schema")
            .and_then(Value::as_str)
            .unwrap_or_default();
        if schema_str != "dr-readiness-proof-v1" {
            proof_errors.push(format!("invalid-dr-readiness-proof-schema:{schema_str}"));
        }
        let p_pre = proof
            .get("workspaceDigestBefore")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let p_post = proof
            .get("workspaceDigestAfter")
            .and_then(Value::as_str)
            .unwrap_or_default();
        if !is_plain_sha256(p_pre) || !is_plain_sha256(p_post) || p_pre != p_post {
            proof_errors.push("drill-workspace-digest-mismatch".to_string());
        }
        for bf in [
            "commitVerified",
            "receiptVerified",
            "ageVerified",
            "cleanupCompleted",
        ] {
            if proof.get(bf) != Some(&Value::Bool(true)) {
                proof_errors.push(format!("{bf}-not-true"));
            }
        }
        if !proof_errors.is_empty() {
            return (
                false,
                json!({
                    "executionVerified": false,
                    "error": format!("dr-drill-proof-invalid:{}", proof_errors.join(",")),
                    "proofErrors": proof_errors,
                }),
            );
        }
        let action_id = action.get("actionId").map(value_str).unwrap_or_default();
        let result_drill_id = execution_result
            .get("drillId")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        let action_bid = action_param_str(action, "backupId", &["backupId"]);
        let result_bid = execution_result
            .get("backupId")
            .or_else(|| execution_result.get("testedBackupId"))
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        let expected_bid = if !action_bid.is_empty() {
            action_bid.clone()
        } else {
            result_bid.clone()
        };
        for (field, expected) in [
            ("resilienceActionId", action_id.as_str()),
            ("drillId", result_drill_id.as_str()),
            ("backupId", expected_bid.as_str()),
        ] {
            let observed = proof
                .get(field)
                .filter(|v| python_truthy(v))
                .map(value_str)
                .unwrap_or_default();
            if expected.is_empty() || observed != expected {
                return (
                    false,
                    json!({
                        "executionVerified": false,
                        "error": format!(
                            "dr-drill-proof-{field}-mismatch:expected={},observed={}",
                            if expected.is_empty() { "<missing>" } else { expected },
                            if observed.is_empty() { "<missing>" } else { &observed },
                        ),
                    }),
                );
            }
        }
        if !action_bid.is_empty() && !result_bid.is_empty() && action_bid != result_bid {
            return (
                false,
                json!({
                    "executionVerified": false,
                    "error": format!("dr-drill-result-backupId-mismatch:action={action_bid},result={result_bid}"),
                }),
            );
        }
        return (
            true,
            json!({
                "executionVerified": true,
                "drillId": result_drill_id,
                "backupId": expected_bid,
                "resilienceActionId": action_id,
                "proofSchema": schema_str,
                "verifiedAt": now_iso,
            }),
        );
    }

    (
        false,
        json!({"executionVerified": false, "error": format!("unsupported-verification-type:{act_type}")}),
    )
}

fn find_matching_risk<'a>(subject: &Value, snapshot: &'a Value) -> Option<&'a Value> {
    let rs = subject.as_object()?;
    let s_type = rs
        .get("type")
        .filter(|v| python_truthy(v))
        .map(|v| value_str(v).to_uppercase())
        .unwrap_or_default();
    let s_pol = rs
        .get("policyId")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();
    let s_bid = rs
        .get("backupId")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();
    let s_tid = rs
        .get("targetId")
        .or_else(|| rs.get("target"))
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();
    let s_fd = rs
        .get("failureDomain")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();

    snapshot
        .get("risks")
        .and_then(Value::as_array)?
        .iter()
        .find(|r| {
            let r_type = r
                .get("type")
                .filter(|v| python_truthy(v))
                .map(|v| value_str(v).to_uppercase())
                .unwrap_or_default();
            let r_pol = r
                .get("policyId")
                .filter(|v| python_truthy(v))
                .map(value_str)
                .unwrap_or_default();
            let r_bid = r
                .get("backupId")
                .filter(|v| python_truthy(v))
                .map(value_str)
                .unwrap_or_default();
            let r_tid = r
                .get("targetId")
                .or_else(|| r.get("target"))
                .filter(|v| python_truthy(v))
                .map(value_str)
                .unwrap_or_default();
            let r_fd = r
                .get("failureDomain")
                .filter(|v| python_truthy(v))
                .map(value_str)
                .unwrap_or_default();
            (s_type.is_empty() || r_type == s_type)
                && (s_pol.is_empty() || r_pol == s_pol)
                && (s_bid.is_empty() || r_bid == s_bid)
                && (s_tid.is_empty() || r_tid == s_tid)
                && (s_fd.is_empty() || r_fd == s_fd)
        })
}

fn verify_scoped_risk_reduction_db(
    action: &Value,
    risk_before_snap: &Value,
    risk_after_snap: &Value,
) -> (bool, Value) {
    let raw_subj = action.get("riskSubject").filter(|s| {
        s.as_object()
            .and_then(|o| o.get("type"))
            .is_some_and(python_truthy)
    });
    let act_type = action
        .get("type")
        .filter(|v| python_truthy(v))
        .map(|v| value_str(v).to_uppercase())
        .unwrap_or_default();
    let risk_subject = if let Some(s) = raw_subj {
        s.clone()
    } else if act_type == "CREATE_REPAIR_JOB" {
        json!({
            "type": "REPLICA_LAG",
            "policyId": action_param_str(action, "policyId", &["policyId"]),
            "backupId": action_param_str(action, "backupId", &["backupId"]),
            "targetId": action_param_str(action, "destTargetId", &["destination"]),
        })
    } else if act_type == "CREATE_REBALANCE_JOB" {
        json!({
            "type": "CAPACITY_EXHAUSTION",
            "targetId": action_param_str(action, "sourceTargetId", &["source", "target"]),
            "policyId": action_param_str(action, "policyId", &["policyId"]),
        })
    } else if act_type == "START_DR_DRILL" {
        json!({
            "type": "DR_STALENESS",
            "policyId": action_param_str(action, "policyId", &["policyId"]),
        })
    } else {
        json!({"type": act_type})
    };

    let risk_before = find_matching_risk(&risk_subject, risk_before_snap);
    let Some(rb) = risk_before else {
        return (
            false,
            json!({
                "riskSubject": risk_subject,
                "effectObserved": false,
                "reason": "target-risk-subject-not-observed-before",
            }),
        );
    };
    let risk_after = find_matching_risk(&risk_subject, risk_after_snap);
    let sev_before = rb
        .get("severity")
        .filter(|v| python_truthy(v))
        .map(|v| value_str(v).to_lowercase())
        .unwrap_or_else(|| "warning".to_string());
    let sev_after = risk_after
        .and_then(|r| r.get("severity"))
        .filter(|v| python_truthy(v))
        .map(|v| value_str(v).to_lowercase())
        .unwrap_or_else(|| "healthy".to_string());
    let ord_before = severity_rank(&sev_before);
    let ord_after = severity_rank(&sev_after);

    if risk_after.is_none() || sev_after == "healthy" {
        return (
            true,
            json!({
                "riskSubject": risk_subject,
                "severityBefore": sev_before,
                "severityAfter": sev_after,
                "orderBefore": ord_before,
                "orderAfter": ord_after,
                "effectObserved": true,
                "reason": "target-risk-cleared-or-healthy",
            }),
        );
    }
    if ord_after < ord_before {
        return (
            true,
            json!({
                "riskSubject": risk_subject,
                "severityBefore": sev_before,
                "severityAfter": sev_after,
                "orderBefore": ord_before,
                "orderAfter": ord_after,
                "effectObserved": true,
                "reason": format!("target-risk-severity-decreased:{sev_before}->{sev_after}"),
            }),
        );
    }
    (
        false,
        json!({
            "riskSubject": risk_subject,
            "severityBefore": sev_before,
            "severityAfter": sev_after,
            "orderBefore": ord_before,
            "orderAfter": ord_after,
            "effectObserved": false,
            "reason": format!("target-risk-not-improved:before={sev_before},after={sev_after}"),
        }),
    )
}

pub(crate) fn compensate_action_db(
    root: &Path,
    action_id: &str,
    error_msg: &str,
    effect_class: &str,
    execution_epoch: Option<i64>,
    claim_token: Option<&str>,
) -> Result<Value, AppError> {
    let conn = open_journal_rw(root)?;
    let action = get_action_from_conn(&conn, action_id)?
        .ok_or_else(|| not_found_error(format!("Action '{action_id}' not found")))?;
    drop(conn);

    if execution_epoch.is_some() != claim_token.is_some() {
        return Err(AppError::invalid_payload(
            "Compensation fencing requires both execution_epoch and claim_token",
        ));
    }
    if let (Some(exp_ep), Some(exp_tok)) = (execution_epoch, claim_token) {
        let cur_ep = action
            .get("executionEpoch")
            .and_then(Value::as_i64)
            .unwrap_or(0);
        let cur_tok = action
            .get("claimToken")
            .and_then(Value::as_str)
            .unwrap_or_default();
        if cur_ep != exp_ep || cur_tok != exp_tok {
            return Err(conflict_error(format!(
                "Action '{action_id}' lease lost before compensation"
            )));
        }
    }

    let (target_state, comp_state, compensated_handle) = if effect_class == "NO_EFFECT" {
        ("FAILED_BEFORE_EFFECT", "NONE", None)
    } else if effect_class == "CANCELABLE" {
        let handle = action.get("effectHandle").and_then(Value::as_object);
        let kind = handle
            .and_then(|h| h.get("kind"))
            .and_then(Value::as_str)
            .unwrap_or_default();
        let mut cancellation = json!({"status": "unknown", "reason": "cancelable-effect-handle-missing-or-unsupported"});
        if kind == "repair" {
            if let Some(rid) = handle
                .and_then(|h| h.get("repairId"))
                .filter(|v| python_truthy(v))
                .map(value_str)
            {
                if let Some(mut j) = read_repair_job_fs(root, &rid) {
                    let ph = j
                        .get("phase")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_string();
                    if ph == "cancelled" {
                        cancellation = json!({"status": "cancelled", "repairId": rid, "phase": ph});
                    } else if ph == "queued" {
                        j["phase"] = Value::String("cancelled".to_string());
                        j["cancellationReason"] = Value::String(error_msg.to_string());
                        j["cancelledAt"] = Value::String(now_unix_secs_and_iso().1);
                        let _ = write_repair_job_fs(root, &j);
                        cancellation =
                            json!({"status": "cancelled", "repairId": rid, "phase": "cancelled"});
                    } else {
                        cancellation =
                            json!({"status": "not-cancelable", "repairId": rid, "phase": ph});
                    }
                } else {
                    cancellation = json!({"status": "unknown", "repairId": rid, "reason": "repair-job-not-found"});
                }
            }
        } else if kind == "rebalance" {
            if let Some(jid) = handle
                .and_then(|h| h.get("jobId"))
                .filter(|v| python_truthy(v))
                .map(value_str)
            {
                if let Some(mut j) = read_rebalance_job_fs(root, &jid) {
                    let ph = j
                        .get("phase")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_string();
                    if ph == "cancelled" {
                        cancellation = json!({"status": "cancelled", "jobId": jid, "phase": ph});
                    } else if ph == "pending" {
                        j["phase"] = Value::String("cancelled".to_string());
                        j["cancellationReason"] = Value::String(error_msg.to_string());
                        j["cancelledAt"] = Value::String(now_unix_secs_and_iso().1);
                        let _ = write_rebalance_job_fs(root, &j);
                        cancellation =
                            json!({"status": "cancelled", "jobId": jid, "phase": "cancelled"});
                    } else {
                        cancellation =
                            json!({"status": "not-cancelable", "jobId": jid, "phase": ph});
                    }
                } else {
                    cancellation = json!({"status": "unknown", "jobId": jid, "reason": "rebalance-job-not-found"});
                }
            }
        }
        let c_status = cancellation
            .get("status")
            .and_then(Value::as_str)
            .unwrap_or("unknown");
        let (t_st, c_st) = if c_status == "cancelled" {
            ("COMPENSATED", "JOB_CANCELLED")
        } else if c_status == "not-cancelable" {
            ("COMPENSATION_REQUIRED", "JOB_NOT_CANCELABLE")
        } else {
            ("EFFECT_UNKNOWN", "REMOTE_EFFECT_UNCERTAIN")
        };
        let mut comp_h = handle.cloned().unwrap_or_default();
        let mut sub = Map::new();
        for key in ["status", "phase", "reason", "repairId", "jobId"] {
            if let Some(v) = cancellation.get(key) {
                sub.insert(key.to_string(), v.clone());
            }
        }
        comp_h.insert("cancellationResult".to_string(), Value::Object(sub));
        (t_st, c_st, Some(Value::Object(comp_h)))
    } else if effect_class == "COMPENSATABLE" {
        ("COMPENSATION_REQUIRED", "COMPENSATOR_NOT_IMPLEMENTED", None)
    } else if effect_class == "EFFECT_UNKNOWN" {
        ("EFFECT_UNKNOWN", "REMOTE_EFFECT_UNCERTAIN", None)
    } else {
        ("NEEDS_OPERATOR", "MANUAL_INTERVENTION_REQUIRED", None)
    };

    update_action_state_db(
        root,
        action_id,
        target_state,
        &ActionStateUpdate {
            execution_epoch,
            claim_token,
            effect_class: Some(effect_class),
            compensation_state: Some(comp_state),
            effect_handle: compensated_handle,
            error: Some(error_msg),
            ..Default::default()
        },
    )
}

pub(crate) fn execute_autonomous_action_db(
    root: &Path,
    action_id: &str,
    instance_id: &str,
    lease_seconds: u64,
) -> Result<Value, AppError> {
    let conn = open_journal_rw(root)?;
    let action_pre = get_action_from_conn(&conn, action_id)?
        .ok_or_else(|| not_found_error(format!("Action '{action_id}' not found in journal")))?;
    drop(conn);

    if let Err(adm_reason) = validate_action_admission(root, &action_pre) {
        update_action_state_db(
            root,
            action_id,
            "BLOCKED",
            &ActionStateUpdate {
                error: Some(&adm_reason),
                ..Default::default()
            },
        )?;
        return Err(forbidden_error(format!(
            "Autonomous action execution blocked: {adm_reason}"
        )));
    }

    let (claimed, claimed_opt, claim_reason) =
        admit_and_claim_action_db(root, action_id, instance_id, lease_seconds)?;
    let Some(action) = claimed_opt.filter(|_| claimed) else {
        let cur_state = action_pre
            .get("state")
            .and_then(Value::as_str)
            .unwrap_or("unknown");
        let is_budget_rejection =
            claim_reason.starts_with("max-") || claim_reason.starts_with("hourly-");
        if is_budget_rejection {
            update_action_state_db(
                root,
                action_id,
                "PENDING",
                &ActionStateUpdate {
                    error: Some(&claim_reason),
                    ..Default::default()
                },
            )?;
            return Err(rate_limited_error(format!(
                "Action '{action_id}' could not be claimed (state: {cur_state}, reason: {claim_reason})"
            )));
        }
        return Err(conflict_error(format!(
            "Action '{action_id}' could not be claimed (state: {cur_state}, reason: {claim_reason})"
        )));
    };

    let epoch = action
        .get("executionEpoch")
        .and_then(Value::as_i64)
        .unwrap_or(1);
    let token = action
        .get("claimToken")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let act_type = action
        .get("type")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_uppercase();

    let mut skip_execution_mutation = false;
    let mut resume_job_info = Value::Null;
    let mut result_payload = json!({});
    let mut current_effect = action
        .get("effectClass")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .unwrap_or("NO_EFFECT")
        .to_string();

    if action.get("state").and_then(Value::as_str) == Some("RECONCILING") {
        match reconcile_action_effect_db(root, &action) {
            ReconcileDirective::EffectUnknown(details) => {
                let err_msg = details
                    .get("error")
                    .and_then(Value::as_str)
                    .unwrap_or("effect reconciliation failed closed");
                update_action_state_db(
                    root,
                    action_id,
                    "EFFECT_UNKNOWN",
                    &ActionStateUpdate {
                        execution_epoch: Some(epoch),
                        claim_token: Some(&token),
                        compensation_state: Some("REMOTE_EFFECT_UNCERTAIN"),
                        error: Some(err_msg),
                        ..Default::default()
                    },
                )?;
                return Err(AppError {
                    status: 500,
                    code: codes::INTERNAL,
                    message: format!("Action effect reconciliation failed closed: {err_msg}"),
                });
            }
            ReconcileDirective::TriggerCompensation(details) => {
                let err_msg = details
                    .get("error")
                    .and_then(Value::as_str)
                    .unwrap_or("reconciliation-triggered-compensation");
                let ec = action
                    .get("effectClass")
                    .and_then(Value::as_str)
                    .filter(|s| !s.is_empty())
                    .unwrap_or("CANCELABLE");
                compensate_action_db(root, action_id, err_msg, ec, Some(epoch), Some(&token))?;
                return Err(AppError {
                    status: 500,
                    code: codes::INTERNAL,
                    message: format!(
                        "Action compensation triggered during reconciliation: {err_msg}"
                    ),
                });
            }
            ReconcileDirective::AdvanceToVerifying(details) => {
                result_payload = details;
                current_effect = action
                    .get("effectClass")
                    .and_then(Value::as_str)
                    .filter(|s| !s.is_empty())
                    .unwrap_or("CANCELABLE")
                    .to_string();
                skip_execution_mutation = true;
            }
            ReconcileDirective::ResumeExecution(details) => {
                resume_job_info = details;
            }
            ReconcileDirective::ResumeSimulating | ReconcileDirective::RecreateEffect => {}
        }
    }

    let (before_secs, before_iso) = now_unix_secs_and_iso();
    let mut risk_before_snapshot =
        evaluate_risk_snapshot_readonly(root, false, before_secs, &before_iso)?;
    observe_risk_snapshot_and_slo(root, &mut risk_before_snapshot, before_secs, &before_iso)?;
    let risk_before_digest = risk_before_snapshot
        .get("riskDigest")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();

    let mut compensated = false;
    let inner_res: Result<Value, AppError> = (|| {
        if !skip_execution_mutation {
            if let Err((cleared, fresh_reason)) =
                check_action_freshness(&action, &risk_before_snapshot)
            {
                let target_st = if cleared {
                    "SKIPPED_NO_LONGER_NEEDED"
                } else {
                    "REPLAN_REQUIRED"
                };
                return update_action_state_db(
                    root,
                    action_id,
                    target_st,
                    &ActionStateUpdate {
                        execution_epoch: Some(epoch),
                        claim_token: Some(&token),
                        risk_before_digest: Some(&risk_before_digest),
                        error: Some(&fresh_reason),
                        ..Default::default()
                    },
                );
            }

            if let Err(sim_err) = simulate_action_db(root, &action) {
                update_action_state_db(
                    root,
                    action_id,
                    "BLOCKED",
                    &ActionStateUpdate {
                        execution_epoch: Some(epoch),
                        claim_token: Some(&token),
                        error: Some(&sim_err),
                        ..Default::default()
                    },
                )?;
                return Err(AppError::invalid_payload(format!(
                    "Action precondition simulation failed: {sim_err}"
                )));
            }

            update_action_state_db(
                root,
                action_id,
                "EXECUTING",
                &ActionStateUpdate {
                    execution_epoch: Some(epoch),
                    claim_token: Some(&token),
                    effect_class: Some("NO_EFFECT"),
                    ..Default::default()
                },
            )?;
            current_effect = "NO_EFFECT".to_string();

            if act_type == "CREATE_REBALANCE_JOB" {
                let policy_id = action_param_str(&action, "policyId", &["policyId"]);
                let backup_id = action_param_str(&action, "backupId", &["backupId"]);
                let source_id = action_param_str(&action, "sourceTargetId", &["source", "target"]);
                let dest_id = action_param_str(&action, "destTargetId", &["destination"]);
                let reason = {
                    let r = action_param_str(&action, "reason", &[]);
                    if r.is_empty() {
                        "resilience-planner-rebalance".to_string()
                    } else {
                        r
                    }
                };
                let job = if let Some(jid) = resume_job_info
                    .get("jobId")
                    .filter(|v| python_truthy(v))
                    .map(value_str)
                {
                    resume_job_info
                        .get("job")
                        .filter(|v| v.is_object())
                        .cloned()
                        .or_else(|| read_rebalance_job_fs(root, &jid))
                        .unwrap_or_else(|| json!({"jobId": jid}))
                } else {
                    create_rebalance_job_fs(
                        root,
                        &policy_id,
                        &backup_id,
                        &source_id,
                        &dest_id,
                        &reason,
                        Some(action_id),
                    )?
                };
                let jid = job
                    .get("jobId")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_string();
                current_effect = "CANCELABLE".to_string();
                let effect_handle = json!({"kind": "rebalance", "jobId": jid});
                result_payload = json!({"job": job, "jobId": jid});
                update_action_state_db(
                    root,
                    action_id,
                    "EXECUTING",
                    &ActionStateUpdate {
                        execution_epoch: Some(epoch),
                        claim_token: Some(&token),
                        effect_class: Some(&current_effect),
                        effect_handle: Some(effect_handle),
                        ..Default::default()
                    },
                )?;
                match execute_rebalance_job_native(root, &jid, instance_id) {
                    Ok(reb_res) => {
                        result_payload["executionStatus"] =
                            reb_res.get("status").cloned().unwrap_or(Value::Null);
                        result_payload["rebalanceResult"] = reb_res;
                    }
                    Err(e) if e.status == 409 => return Err(e),
                    Err(e) => {
                        result_payload["error"] = Value::String(e.message);
                    }
                }
            } else if act_type == "CREATE_REPAIR_JOB" {
                let policy_id = action_param_str(&action, "policyId", &["policyId"]);
                let backup_id = action_param_str(&action, "backupId", &["backupId"]);
                let source_id = action_param_str(&action, "sourceTargetId", &["source"]);
                let dest_id = action_param_str(&action, "destTargetId", &["destination", "target"]);
                let job = if let Some(rid) = resume_job_info
                    .get("repairId")
                    .filter(|v| python_truthy(v))
                    .map(value_str)
                {
                    resume_job_info
                        .get("job")
                        .filter(|v| v.is_object())
                        .cloned()
                        .or_else(|| read_repair_job_fs(root, &rid))
                        .unwrap_or_else(|| json!({"repairId": rid}))
                } else {
                    create_repair_job_fs(
                        root,
                        &policy_id,
                        &backup_id,
                        Some(&source_id),
                        &dest_id,
                        Some(action_id),
                    )?
                };
                let rid = job
                    .get("repairId")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_string();
                current_effect = "CANCELABLE".to_string();
                let effect_handle = json!({"kind": "repair", "repairId": rid});
                result_payload = json!({"job": job, "repairId": rid});
                update_action_state_db(
                    root,
                    action_id,
                    "EXECUTING",
                    &ActionStateUpdate {
                        execution_epoch: Some(epoch),
                        claim_token: Some(&token),
                        effect_class: Some(&current_effect),
                        effect_handle: Some(effect_handle),
                        ..Default::default()
                    },
                )?;
                match execute_replica_repair_native(
                    root,
                    &rid,
                    Some(source_id.as_str()).filter(|s| !s.is_empty()),
                    instance_id,
                ) {
                    Ok(rep_res) => {
                        result_payload["executionStatus"] = rep_res
                            .get("status")
                            .or_else(|| rep_res.get("phase"))
                            .cloned()
                            .unwrap_or(Value::Null);
                        result_payload["repairResult"] = rep_res;
                    }
                    Err(e) if e.status == 409 => return Err(e),
                    Err(e) => {
                        result_payload["error"] = Value::String(e.message);
                    }
                }
            } else if act_type == "START_DR_DRILL" {
                let mut payload = Map::new();
                payload.insert(
                    "resilienceActionId".to_string(),
                    Value::String(action_id.to_string()),
                );
                let bid = action_param_str(&action, "backupId", &["backupId"]);
                if !bid.is_empty() {
                    payload.insert("backupId".to_string(), Value::String(bid));
                }
                let tid = action_param_str(&action, "targetId", &["target"]);
                if !tid.is_empty() {
                    payload.insert("targetId".to_string(), Value::String(tid));
                }
                let drill_res = execute_run_dr_drill(&payload)?;
                current_effect = "COMPENSATABLE".to_string();
                let drill_id = drill_res.get("drillId").cloned().unwrap_or(Value::Null);
                let eh = json!({
                    "kind": "drill",
                    "resilienceActionId": action_id,
                    "drillId": drill_id,
                });
                result_payload = drill_res;
                update_action_state_db(
                    root,
                    action_id,
                    "EXECUTING",
                    &ActionStateUpdate {
                        execution_epoch: Some(epoch),
                        claim_token: Some(&token),
                        effect_class: Some(&current_effect),
                        effect_handle: Some(eh),
                        ..Default::default()
                    },
                )?;
            } else {
                return Err(AppError::invalid_payload(format!(
                    "Unsupported action execution type: {act_type}"
                )));
            }
        }

        update_action_state_db(
            root,
            action_id,
            "VERIFYING",
            &ActionStateUpdate {
                execution_epoch: Some(epoch),
                claim_token: Some(&token),
                effect_class: Some(&current_effect),
                ..Default::default()
            },
        )?;
        let (verified_ok, verif_details) = verify_action_outcome_db(root, &action, &result_payload);
        if !verified_ok {
            let err = verif_details
                .get("error")
                .and_then(Value::as_str)
                .unwrap_or("post-condition-outcome-verification-failed");
            compensated = true;
            compensate_action_db(
                root,
                action_id,
                err,
                &current_effect,
                Some(epoch),
                Some(&token),
            )?;
            return Err(AppError {
                status: 500,
                code: codes::INTERNAL,
                message: format!("Outcome verification failed: {err}"),
            });
        }

        update_action_state_db(
            root,
            action_id,
            "ASSESSING_EFFECT",
            &ActionStateUpdate {
                execution_epoch: Some(epoch),
                claim_token: Some(&token),
                effect_class: Some(&current_effect),
                ..Default::default()
            },
        )?;
        let (after_secs, after_iso) = now_unix_secs_and_iso();
        let mut risk_after_snapshot =
            evaluate_risk_snapshot_readonly(root, false, after_secs, &after_iso)?;
        observe_risk_snapshot_and_slo(root, &mut risk_after_snapshot, after_secs, &after_iso)?;
        let risk_after_digest = risk_after_snapshot
            .get("riskDigest")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();

        let (effect_reduced, reduction_details) =
            verify_scoped_risk_reduction_db(&action, &risk_before_snapshot, &risk_after_snapshot);
        if !effect_reduced {
            let err = reduction_details
                .get("reason")
                .and_then(Value::as_str)
                .unwrap_or("target-risk-not-improved");
            compensated = true;
            compensate_action_db(
                root,
                action_id,
                err,
                &current_effect,
                Some(epoch),
                Some(&token),
            )?;
            return Err(AppError {
                status: 500,
                code: codes::INTERNAL,
                message: format!("Scoped risk reduction failed: {err}"),
            });
        }

        let input_rd = action
            .get("inputRiskDigest")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_else(|| risk_before_digest.clone());
        let decision_proof = json!({
            "riskDigest": input_rd,
            "riskBeforeDigest": risk_before_digest,
            "riskAfterDigest": risk_after_digest,
            "riskBefore": {"overallRisk": risk_before_snapshot.get("overallRisk").cloned().unwrap_or(Value::Null)},
            "riskAfter": {"overallRisk": risk_after_snapshot.get("overallRisk").cloned().unwrap_or(Value::Null)},
            "policyVersion": 1,
            "actionAllowed": true,
            "simulationPassed": true,
            "executionVerified": true,
            "effectObserved": reduction_details.get("effectObserved") == Some(&Value::Bool(true)),
            "scopedRiskSubject": reduction_details.get("riskSubject").cloned().unwrap_or(Value::Null),
            "severityBefore": reduction_details.get("severityBefore").cloned().unwrap_or(Value::Null),
            "severityAfter": reduction_details.get("severityAfter").cloned().unwrap_or(Value::Null),
            "executedActionType": act_type,
            "actionId": action_id,
            "planId": action.get("planId").cloned().unwrap_or(Value::Null),
        });

        update_action_state_db(
            root,
            action_id,
            "SUCCEEDED",
            &ActionStateUpdate {
                execution_epoch: Some(epoch),
                claim_token: Some(&token),
                effect_class: Some(&current_effect),
                result: Some(result_payload.clone()),
                verification: Some(verif_details),
                proof: Some(decision_proof),
                risk_before_digest: Some(&risk_before_digest),
                risk_after_digest: Some(&risk_after_digest),
                ..Default::default()
            },
        )
    })();

    if let Err(err) = &inner_res {
        if !compensated && err.status >= 500 {
            let _ = compensate_action_db(
                root,
                action_id,
                &err.message,
                &current_effect,
                Some(epoch),
                Some(&token),
            );
        }
    }
    inner_res
}

#[cfg(test)]
mod unit_tests {
    use super::*;
    use rusqlite::Connection;
    use serde_json::json;

    /// A scratch workspace whose journal database `open_journal_rw` creates for
    /// us - the production schema is applied by that call, so a fixture never
    /// hand-writes a table definition that could drift from the real one.
    struct Journal {
        root: PathBuf,
    }

    impl Journal {
        fn new(tag: &str) -> Self {
            let root = std::env::temp_dir().join(format!(
                "resilience-exec-{tag}-{}-{:?}",
                std::process::id(),
                std::thread::current().id()
            ));
            let _ = std::fs::remove_dir_all(&root);
            std::fs::create_dir_all(&root).unwrap();
            Self { root }
        }

        fn open(&self) -> Connection {
            open_journal_rw(&self.root).expect("journal opens")
        }
    }

    impl Drop for Journal {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.root);
        }
    }

    /// Insert one action row with the columns the rate-limit query reads.
    fn insert_action(
        conn: &Connection,
        action_id: &str,
        action_type: &str,
        state: &str,
        severity_before: Option<&str>,
        risk_subject: Option<&str>,
        created_at: &str,
    ) {
        conn.execute(
            "INSERT INTO resilience_actions
                (action_id, action_type, created_by, state, severity_before,
                 risk_subject_json, created_at, updated_at)
             VALUES (?, ?, 'test', ?, ?, ?, ?, ?)",
            rusqlite::params![
                action_id,
                action_type,
                state,
                severity_before,
                risk_subject,
                created_at,
                created_at
            ],
        )
        .unwrap();
    }

    /// `reconcile_action_effect_db` decides what to do with an action whose
    /// effect was interrupted. `CLAIMED` means the simulation never finished, and
    /// the repair branch keys off the job's own phase.
    #[test]
    fn reconciliation_directs_each_interrupted_state() {
        let journal = Journal::new("reconcile");

        // A CLAIMED action has not started its effect yet.
        assert!(matches!(
            reconcile_action_effect_db(
                &journal.root,
                &json!({"actionId": "a1", "type": "CREATE_REPAIR_JOB", "state": "CLAIMED"})
            ),
            ReconcileDirective::ResumeSimulating
        ));

        // No handle and no effect class means the effect was never created.
        assert!(matches!(
            reconcile_action_effect_db(
                &journal.root,
                &json!({"actionId": "a2", "type": "CREATE_REPAIR_JOB", "state": "EXECUTING"})
            ),
            ReconcileDirective::RecreateEffect
        ));

        // A handle that names no job is uncertain, not recreated.
        assert!(matches!(
            reconcile_action_effect_db(
                &journal.root,
                &json!({
                    "actionId": "a3",
                    "type": "CREATE_REPAIR_JOB",
                    "state": "EXECUTING",
                    "effectHandle": {"kind": "repair", "repairId": "missing"}
                })
            ),
            ReconcileDirective::EffectUnknown(_)
        ));

        // A job in a terminal-good phase advances to verification.
        let repairs = journal.root.join(".backup-replication").join("repairs");
        std::fs::create_dir_all(&repairs).unwrap();
        std::fs::write(
            repairs.join("r1.json"),
            json!({"repairId": "r1", "phase": "complete"}).to_string(),
        )
        .unwrap();
        assert!(matches!(
            reconcile_action_effect_db(
                &journal.root,
                &json!({
                    "actionId": "a4",
                    "type": "CREATE_REPAIR_JOB",
                    "state": "EXECUTING",
                    "effectHandle": {"kind": "repair", "repairId": "r1"}
                })
            ),
            ReconcileDirective::AdvanceToVerifying(_)
        ));

        // A failed job triggers compensation.
        std::fs::write(
            repairs.join("r2.json"),
            json!({"repairId": "r2", "phase": "failed", "error": "disk-gone"}).to_string(),
        )
        .unwrap();
        assert!(matches!(
            reconcile_action_effect_db(
                &journal.root,
                &json!({
                    "actionId": "a5",
                    "type": "CREATE_REPAIR_JOB",
                    "state": "EXECUTING",
                    "effectHandle": {"kind": "repair", "repairId": "r2"}
                })
            ),
            ReconcileDirective::TriggerCompensation(_)
        ));

        // A job still running resumes execution.
        std::fs::write(
            repairs.join("r3.json"),
            json!({"repairId": "r3", "phase": "copying"}).to_string(),
        )
        .unwrap();
        assert!(matches!(
            reconcile_action_effect_db(
                &journal.root,
                &json!({
                    "actionId": "a6",
                    "type": "CREATE_REPAIR_JOB",
                    "state": "EXECUTING",
                    "effectHandle": {"kind": "repair", "repairId": "r3"}
                })
            ),
            ReconcileDirective::ResumeExecution(_)
        ));
    }

    /// A rebalance reconciles through the same phases on its own directory.
    #[test]
    fn reconciliation_handles_rebalance_jobs() {
        let journal = Journal::new("reconcile-rebalance");
        let dir = journal.root.join(".backup-rebalance");
        std::fs::create_dir_all(&dir).unwrap();

        // No job at all, with an effect class that says one should exist.
        assert!(matches!(
            reconcile_action_effect_db(
                &journal.root,
                &json!({
                    "actionId": "j0",
                    "type": "CREATE_REBALANCE_JOB",
                    "state": "EXECUTING",
                    "effectClass": "COMPENSATABLE"
                })
            ),
            ReconcileDirective::EffectUnknown(_)
        ));

        std::fs::write(
            dir.join("jb1.json"),
            json!({"jobId": "jb1", "phase": "complete"}).to_string(),
        )
        .unwrap();
        assert!(matches!(
            reconcile_action_effect_db(
                &journal.root,
                &json!({
                    "actionId": "j1",
                    "type": "CREATE_REBALANCE_JOB",
                    "state": "EXECUTING",
                    "effectHandle": {"kind": "rebalance", "jobId": "jb1"}
                })
            ),
            ReconcileDirective::AdvanceToVerifying(_)
        ));

        std::fs::write(
            dir.join("jb2.json"),
            json!({"jobId": "jb2", "phase": "failed"}).to_string(),
        )
        .unwrap();
        assert!(matches!(
            reconcile_action_effect_db(
                &journal.root,
                &json!({
                    "actionId": "j2",
                    "type": "CREATE_REBALANCE_JOB",
                    "state": "EXECUTING",
                    "effectHandle": {"kind": "rebalance", "jobId": "jb2"}
                })
            ),
            ReconcileDirective::TriggerCompensation(_)
        ));
    }

    /// `compensate_action_db` maps each effect class onto a target state and a
    /// compensation state, and fences the write when an epoch and token are
    /// supplied.
    #[test]
    fn compensation_maps_every_effect_class() {
        let journal = Journal::new("compensate");
        let iso = "2023-11-14T22:13:20Z";

        // Each case seeds its own row, because compensation rewrites the state.
        for (index, (effect_class, want_state, want_comp)) in [
            ("NO_EFFECT", "FAILED_BEFORE_EFFECT", "NONE"),
            (
                "COMPENSATABLE",
                "COMPENSATION_REQUIRED",
                "COMPENSATOR_NOT_IMPLEMENTED",
            ),
            (
                "EFFECT_UNKNOWN",
                "EFFECT_UNKNOWN",
                "REMOTE_EFFECT_UNCERTAIN",
            ),
            (
                "SOMETHING_ELSE",
                "NEEDS_OPERATOR",
                "MANUAL_INTERVENTION_REQUIRED",
            ),
        ]
        .into_iter()
        .enumerate()
        {
            let id = format!("a{index}");
            {
                let conn = journal.open();
                insert_action(
                    &conn,
                    &id,
                    "CREATE_REPAIR_JOB",
                    "EXECUTING",
                    None,
                    None,
                    iso,
                );
            }
            let out =
                compensate_action_db(&journal.root, &id, "boom", effect_class, None, None).unwrap();
            assert_eq!(out["state"], want_state, "{effect_class}");
            assert_eq!(out["compensationState"], want_comp, "{effect_class}");
            // The row projection exposes `effectClass`, which is what the caller
            // passed in; the error text is stored but not projected here.
            assert_eq!(out["effectClass"], effect_class, "{effect_class}");
        }

        // A `CANCELABLE` effect with no usable handle is uncertain, not cancelled.
        {
            let conn = journal.open();
            insert_action(
                &conn,
                "c-cancel",
                "CREATE_REPAIR_JOB",
                "EXECUTING",
                None,
                None,
                iso,
            );
        }
        let out = compensate_action_db(&journal.root, "c-cancel", "boom", "CANCELABLE", None, None)
            .unwrap();
        assert_eq!(out["state"], "EFFECT_UNKNOWN");
        assert_eq!(out["compensationState"], "REMOTE_EFFECT_UNCERTAIN");
    }

    /// An unknown action is a 404, and half a fence is a 400.
    #[test]
    fn compensation_refuses_missing_rows_and_partial_fences() {
        let journal = Journal::new("compensate-refuse");
        let iso = "2023-11-14T22:13:20Z";

        // No such row.
        let error = compensate_action_db(&journal.root, "absent", "boom", "NO_EFFECT", None, None)
            .unwrap_err();
        assert_eq!(error.status, 404, "{}", error.message);

        {
            let conn = journal.open();
            insert_action(
                &conn,
                "a1",
                "CREATE_REPAIR_JOB",
                "EXECUTING",
                None,
                None,
                iso,
            );
        }
        // An epoch without a token is refused before any write.
        let error = compensate_action_db(&journal.root, "a1", "boom", "NO_EFFECT", Some(1), None)
            .unwrap_err();
        assert_eq!(error.status, 400);
        assert!(
            error.message.contains("execution_epoch and claim_token"),
            "{}",
            error.message
        );
        // A token without an epoch is refused the same way.
        let error =
            compensate_action_db(&journal.root, "a1", "boom", "NO_EFFECT", None, Some("tok"))
                .unwrap_err();
        assert_eq!(error.status, 400);
    }

    /// A full fence whose epoch or token does not match the stored row is a 409.
    #[test]
    fn compensation_rejects_a_stale_fence() {
        let journal = Journal::new("compensate-fence");
        let iso = "2023-11-14T22:13:20Z";
        {
            let conn = journal.open();
            insert_action(
                &conn,
                "a1",
                "CREATE_REPAIR_JOB",
                "EXECUTING",
                None,
                None,
                iso,
            );
        }
        // The stored row has epoch 0 and no claim token, so a non-zero epoch with
        // any token cannot match.
        let error = compensate_action_db(
            &journal.root,
            "a1",
            "boom",
            "NO_EFFECT",
            Some(99),
            Some("tok"),
        )
        .unwrap_err();
        assert_eq!(error.status, 409, "{}", error.message);
        assert!(
            error.message.contains("lease lost before compensation"),
            "{}",
            error.message
        );
    }

    /// `validate_action_admission` enforces the safety floor before an action is
    /// written: a type is required, a `NEVER_AUTONOMOUS` type needs an explicit
    /// approval, and a non-autonomous action does too.
    #[test]
    fn admission_validation_covers_the_safety_floor() {
        let journal = Journal::new("validate");

        // No type at all.
        assert_eq!(
            validate_action_admission(&journal.root, &json!({})),
            Err("missing-action-type".to_string())
        );
        // A whitespace-only type is *not* treated as missing: the check only
        // asks whether the uppercased value is empty, and `"  "` is not. It
        // therefore falls through to the autonomy rules and is refused as an
        // unknown, non-autonomous type - recorded here rather than assumed.
        assert_eq!(
            validate_action_admission(&journal.root, &json!({"type": "  "})),
            Err("action-  -not-autonomous-approval-required".to_string())
        );
        // A never-autonomous type is forbidden without an approval flag.
        for refused in [
            "PRIMARY_PROMOTION",
            "POLICY_CHANGE",
            "COPY_DELETION",
            "TOPOLOGY_MUTATION",
        ] {
            assert_eq!(
                validate_action_admission(&journal.root, &json!({"type": refused})),
                Err(format!("action-{refused}-forbidden-by-safety-floor")),
                "{refused}"
            );
            // With `approved` it passes, because the floor is satisfied.
            assert_eq!(
                validate_action_admission(
                    &journal.root,
                    &json!({"type": refused, "approved": true})
                ),
                Ok(()),
                "{refused} with approval"
            );
        }
        // A routine action is admitted with no flags at all.
        assert_eq!(
            validate_action_admission(&journal.root, &json!({"type": "START_DR_DRILL"})),
            Ok(())
        );
        // Asking for approval on an autonomous action is contradictory.
        assert_eq!(
            validate_action_admission(
                &journal.root,
                &json!({"type": "START_DR_DRILL", "requiresApproval": true})
            ),
            Err("action-START_DR_DRILL-requires-explicit-approval".to_string())
        );
        // An unknown type is not autonomous, so it needs an approval.
        assert_eq!(
            validate_action_admission(&journal.root, &json!({"type": "SOMETHING_ELSE"})),
            Err("action-SOMETHING_ELSE-not-autonomous-approval-required".to_string())
        );
        assert_eq!(
            validate_action_admission(
                &journal.root,
                &json!({"type": "SOMETHING_ELSE", "approved": true})
            ),
            Ok(())
        );
        // The type is uppercased before every check.
        assert_eq!(
            validate_action_admission(&journal.root, &json!({"type": "start_dr_drill"})),
            Ok(())
        );
    }

    /// `record_action_intent_db` mints an id when the action has none, and the
    /// initial state depends on whether the action is autonomous: an approval
    /// requirement or a never-autonomous type lands in `APPROVAL_REQUIRED`.
    #[test]
    fn recording_an_intent_chooses_the_initial_state() {
        let journal = Journal::new("intent");
        let iso = "2023-11-14T22:13:20Z";
        // A syntactically valid 64-hex digest; the recorder does not verify it.
        let digest = "a".repeat(64);

        // A routine action with no approval flag is PENDING.
        let pending = record_action_intent_db(
            &journal.root,
            &json!({"actionId": "a1", "type": "START_DR_DRILL", "parameters": {}}),
            "operator",
            Some("plan-1"),
            &digest,
            &digest,
            iso,
        )
        .unwrap();
        assert_eq!(pending["actionId"], "a1");
        assert_eq!(pending["state"], "PENDING");
        assert_eq!(pending["type"], "START_DR_DRILL");
        assert_eq!(pending["planId"], "plan-1");

        // `requiresApproval` forces APPROVAL_REQUIRED.
        let approval = record_action_intent_db(
            &journal.root,
            &json!({
                "actionId": "a2",
                "type": "START_DR_DRILL",
                "requiresApproval": true,
                "parameters": {}
            }),
            "operator",
            None,
            &digest,
            &digest,
            iso,
        )
        .unwrap();
        assert_eq!(approval["state"], "APPROVAL_REQUIRED");

        // A never-autonomous type is APPROVAL_REQUIRED even without the flag.
        let promotion = record_action_intent_db(
            &journal.root,
            &json!({"actionId": "a3", "type": "PRIMARY_PROMOTION", "parameters": {}}),
            "operator",
            None,
            &digest,
            &digest,
            iso,
        )
        .unwrap();
        assert_eq!(promotion["state"], "APPROVAL_REQUIRED");

        // An action with no id gets a minted one.
        let minted = record_action_intent_db(
            &journal.root,
            &json!({"type": "START_DR_DRILL", "parameters": {}}),
            "operator",
            None,
            &digest,
            &digest,
            iso,
        )
        .unwrap();
        let minted_id = minted["actionId"].as_str().expect("minted id");
        assert!(minted_id.starts_with("act_"), "{minted_id}");
        assert_eq!(minted_id.len(), "act_".len() + 12);
    }

    /// `admit_and_claim_action_db` claims a PENDING action, refuses a live lease,
    /// takes over an expired one, and reports an unknown id without an error.
    #[test]
    fn admission_claims_pending_and_respects_live_leases() {
        let journal = Journal::new("admit");
        let conn = journal.open();
        let iso = "2023-11-14T22:13:20Z";
        insert_action(
            &conn,
            "a-pending",
            "START_DR_DRILL",
            "PENDING",
            None,
            None,
            iso,
        );
        drop(conn);

        // A PENDING row is claimed and comes back refreshed.
        let (admitted, action, reason) =
            admit_and_claim_action_db(&journal.root, "a-pending", "owner-1", 60).unwrap();
        assert!(admitted, "refused: {reason}");
        assert_eq!(reason, "admitted-and-claimed");
        let claimed = action.expect("the claimed action is returned");
        assert_eq!(claimed["state"], "CLAIMED");
        assert_eq!(claimed["ownerInstanceId"], "owner-1");
        assert_eq!(claimed["executionEpoch"], 1);

        // The same row now holds a live lease, so a second claim is refused.
        let (admitted, action, reason) =
            admit_and_claim_action_db(&journal.root, "a-pending", "owner-2", 60).unwrap();
        assert!(!admitted, "a live lease was taken over");
        // A refusal still returns the current row, so the caller can report it.
        let current = action.expect("the current row is returned on refusal");
        assert_eq!(current["state"], "CLAIMED");
        assert_eq!(current["ownerInstanceId"], "owner-1");
        assert_eq!(reason, "claim-rejected-not-pending-or-active-lease");

        // An unknown id is reported as a refusal, not an error.
        let (admitted, action, reason) =
            admit_and_claim_action_db(&journal.root, "missing", "owner-1", 60).unwrap();
        assert!(!admitted);
        // No row exists, so this refusal really does return nothing.
        assert!(action.is_none(), "an unknown id returned a row");
        assert_eq!(reason, "action-not-found");
    }

    /// A terminal state cannot be claimed at all.
    #[test]
    fn terminal_actions_are_not_claimable() {
        let journal = Journal::new("admit-terminal");
        let iso = "2023-11-14T22:13:20Z";
        // Seed every terminal row first, then probe each one. `admit_and_claim`
        // opens its own connection, so the seeding handle can be dropped here.
        {
            let conn = journal.open();
            for state in ["SUCCEEDED", "FAILED", "BLOCKED", "STALE", "PREEMPTED"] {
                insert_action(
                    &conn,
                    &format!("a-{state}"),
                    "START_DR_DRILL",
                    state,
                    None,
                    None,
                    iso,
                );
            }
        }
        for state in ["SUCCEEDED", "FAILED", "BLOCKED", "STALE", "PREEMPTED"] {
            let (admitted, action, reason) =
                admit_and_claim_action_db(&journal.root, &format!("a-{state}"), "owner", 60)
                    .unwrap();
            assert!(!admitted, "{state} was claimed");
            // The refusal carries the row back, whose state is still terminal.
            let current = action.unwrap_or_else(|| panic!("{state} returned no row"));
            assert_eq!(current["state"], state, "{state}");
            assert_eq!(
                reason, "claim-rejected-not-pending-or-active-lease",
                "{state}"
            );
        }
    }

    /// `read_json_dir_records` keeps object documents, sorts newest-first by
    /// name, skips dotfiles and non-JSON, and tolerates a missing directory.
    #[test]
    fn read_json_dir_records_filters_and_orders() {
        let journal = Journal::new("dir-records");
        let dir = journal.root.join("records");
        // A missing directory is empty rather than an error.
        assert!(read_json_dir_records(&dir).is_empty());
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("a.json"), r#"{"id":"a"}"#).unwrap();
        std::fs::write(dir.join("b.json"), r#"{"id":"b"}"#).unwrap();
        // A non-object document is dropped.
        std::fs::write(dir.join("c.json"), r#"[1,2]"#).unwrap();
        // Malformed JSON is dropped.
        std::fs::write(dir.join("d.json"), "{broken").unwrap();
        // Non-JSON and dotfiles are ignored outright.
        std::fs::write(dir.join("e.txt"), r#"{"id":"e"}"#).unwrap();
        std::fs::write(dir.join(".hidden.json"), r#"{"id":"h"}"#).unwrap();
        // A subdirectory is not a file and is skipped.
        std::fs::create_dir_all(dir.join("nested.json")).unwrap();

        let records = read_json_dir_records(&dir);
        // Only `a` and `b` survive, and the order is reversed-name.
        assert_eq!(records.len(), 2, "got {records:?}");
        assert_eq!(records[0]["id"], "b");
        assert_eq!(records[1]["id"], "a");
    }

    /// The repair and rebalance job readers return `None` for an absent file,
    /// a malformed one and a non-object, and the stored object otherwise.
    #[test]
    fn job_readers_require_a_readable_object() {
        let journal = Journal::new("job-readers");
        let repairs = journal.root.join(".backup-replication").join("repairs");
        std::fs::create_dir_all(&repairs).unwrap();
        let rebalance = journal.root.join(".backup-rebalance");
        std::fs::create_dir_all(&rebalance).unwrap();

        // Absent.
        assert!(read_repair_job_fs(&journal.root, "r1").is_none());
        assert!(read_rebalance_job_fs(&journal.root, "j1").is_none());

        // Present and well formed.
        std::fs::write(
            repairs.join("r1.json"),
            r#"{"repairId":"r1","state":"PENDING"}"#,
        )
        .unwrap();
        std::fs::write(
            rebalance.join("j1.json"),
            r#"{"jobId":"j1","state":"RUNNING"}"#,
        )
        .unwrap();
        assert_eq!(
            read_repair_job_fs(&journal.root, "r1").unwrap()["state"],
            "PENDING"
        );
        assert_eq!(
            read_rebalance_job_fs(&journal.root, "j1").unwrap()["state"],
            "RUNNING"
        );

        // Malformed JSON and a non-object document are both `None`.
        std::fs::write(repairs.join("r2.json"), "{broken").unwrap();
        std::fs::write(repairs.join("r3.json"), "[]").unwrap();
        assert!(read_repair_job_fs(&journal.root, "r2").is_none());
        assert!(read_repair_job_fs(&journal.root, "r3").is_none());
    }

    /// A DR drill needs no simulation at all, an unknown type is refused by
    /// name, and a rebalance with identical endpoints is refused before any
    /// store is opened.
    #[test]
    fn simulate_action_covers_the_store_free_paths() {
        let journal = Journal::new("simulate");
        // A DR drill is always simulable.
        assert_eq!(
            simulate_action_db(&journal.root, &json!({"type": "START_DR_DRILL"})),
            Ok(())
        );
        // An unknown type names itself in the refusal.
        assert_eq!(
            simulate_action_db(&journal.root, &json!({"type": "NOT_A_TYPE"})),
            Err("unsupported-simulation-type:NOT_A_TYPE".to_string())
        );
        // An action with no type at all reports the empty name.
        assert_eq!(
            simulate_action_db(&journal.root, &json!({})),
            Err("unsupported-simulation-type:".to_string())
        );
        // A rebalance whose endpoints match is refused before the target store
        // is consulted, so this needs no registered target.
        assert_eq!(
            simulate_action_db(
                &journal.root,
                &json!({
                    "type": "CREATE_REBALANCE_JOB",
                    "parameters": {"sourceTargetId": "t1", "destTargetId": "t1"}
                })
            ),
            Err("source-destination-identical-or-empty".to_string())
        );
        // Missing endpoints are refused the same way.
        assert_eq!(
            simulate_action_db(
                &journal.root,
                &json!({"type": "CREATE_REBALANCE_JOB", "parameters": {"sourceTargetId": "t1"}})
            ),
            Err("source-destination-identical-or-empty".to_string())
        );
    }

    /// An empty journal admits: no concurrent action and no recent one.
    #[test]
    fn rate_limits_admit_an_empty_journal() {
        let journal = Journal::new("rl-empty");
        let conn = journal.open();
        let (admitted, reason) = check_rate_limits_in_tx(
            &conn,
            &journal.root,
            &json!({"type": "CREATE_REPAIR_JOB", "severity": "warning"}),
            "",
            1_700_000_000,
            "2023-11-14T22:13:20Z",
        )
        .unwrap();
        assert!(admitted, "refused: {reason}");
        assert_eq!(reason, "admitted");
    }

    /// The default `maxConcurrentActions` is 3, so a fourth concurrent action is
    /// refused with the count in the message.
    #[test]
    fn the_concurrent_action_limit_admits_at_most_three() {
        let journal = Journal::new("rl-concurrent");
        let conn = journal.open();
        let now = 1_700_000_000;
        let iso = "2023-11-14T22:13:20Z";
        for index in 0..3 {
            insert_action(
                &conn,
                &format!("a{index}"),
                "CREATE_REPAIR_JOB",
                "EXECUTING",
                Some("warning"),
                None,
                iso,
            );
        }
        let action = json!({"type": "CREATE_REPAIR_JOB", "severity": "warning"});
        // The fourth is refused.
        let (admitted, reason) =
            check_rate_limits_in_tx(&conn, &journal.root, &action, "", now, iso).unwrap();
        assert!(!admitted, "a fourth concurrent action was admitted");
        assert_eq!(reason, "max-concurrent-actions-exceeded:3>=3");
        // Excluding one of the three makes room for it again.
        let (admitted, reason) =
            check_rate_limits_in_tx(&conn, &journal.root, &action, "a0", now, iso).unwrap();
        assert!(admitted, "refused after exclusion: {reason}");
    }

    /// A terminal state does not count toward concurrency.
    #[test]
    fn terminal_actions_do_not_count_toward_concurrency() {
        let journal = Journal::new("rl-terminal");
        let conn = journal.open();
        let iso = "2023-11-14T22:13:20Z";
        for state in ["SUCCEEDED", "FAILED", "BLOCKED", "STALE", "PREEMPTED"] {
            insert_action(
                &conn,
                &format!("a-{state}"),
                "CREATE_REPAIR_JOB",
                state,
                Some("warning"),
                None,
                iso,
            );
        }
        let (admitted, reason) = check_rate_limits_in_tx(
            &conn,
            &journal.root,
            &json!({"type": "CREATE_REPAIR_JOB", "severity": "warning"}),
            "",
            1_700_000_000,
            iso,
        )
        .unwrap();
        assert!(admitted, "refused: {reason}");
    }

    fn conn() -> Connection {
        Connection::open_in_memory().unwrap()
    }

    /// The four keys are derived from `parameters` first and the action's own
    /// fields second, matching
    /// `deepseek_infra.infra.workspace.resilience_resource_locks`.
    #[test]
    fn resource_locks_follow_the_oracle_precedence() {
        // A policy and a backup together lock the backup, not the policy.
        assert_eq!(
            derive_resource_locks_for_action(
                &json!({"parameters": {"policyId": "p1", "backupId": "b1"}})
            ),
            vec!["backup:p1:b1".to_string()]
        );
        // A policy alone locks the policy.
        assert_eq!(
            derive_resource_locks_for_action(&json!({"parameters": {"policyId": "p1"}})),
            vec!["policy:p1".to_string()]
        );
        // Source and destination yield two target locks, deduplicated when equal.
        assert_eq!(
            derive_resource_locks_for_action(&json!({
                "parameters": {"sourceTargetId": "t1", "destTargetId": "t2"}
            })),
            vec!["target:t1".to_string(), "target:t2".to_string()]
        );
        assert_eq!(
            derive_resource_locks_for_action(&json!({"parameters": {"sourceTargetId": "t1"}})),
            vec!["target:t1".to_string()]
        );
        // `parameters` wins over the action fields of the same name.
        assert_eq!(
            derive_resource_locks_for_action(&json!({
                "parameters": {"sourceTargetId": "from-params"},
                "source": "from-action"
            })),
            vec!["target:from-params".to_string()]
        );
        // Without parameters the action fields are used, and `target` is the
        // last fallback for the destination.
        assert_eq!(
            derive_resource_locks_for_action(&json!({"source": "s1", "target": "d1"})),
            vec!["target:d1".to_string(), "target:s1".to_string()]
        );
        // Nothing to lock.
        assert!(derive_resource_locks_for_action(&json!({})).is_empty());
        // A falsy parameter falls through to the action field.
        assert_eq!(
            derive_resource_locks_for_action(&json!({
                "parameters": {"policyId": ""},
                "policyId": "p2"
            })),
            vec!["policy:p2".to_string()]
        );
    }

    /// `action_param_str` prefers `parameters`, ignores falsy values and
    /// returns an empty string when nothing matches.
    #[test]
    fn action_param_lookup_prefers_parameters() {
        let action = json!({"parameters": {"a": "from-params", "empty": ""}, "b": "from-action"});
        assert_eq!(action_param_str(&action, "a", &["b"]), "from-params");
        assert_eq!(action_param_str(&action, "empty", &["b"]), "from-action");
        assert_eq!(action_param_str(&action, "missing", &["b"]), "from-action");
        assert_eq!(action_param_str(&action, "missing", &["absent"]), "");
        // Numbers and booleans render through `value_str`.
        assert_eq!(
            action_param_str(&json!({"parameters": {"n": 7}}), "n", &[]),
            "7"
        );
        assert_eq!(
            action_param_str(&json!({"parameters": {"b": true}}), "b", &[]),
            "True"
        );
    }

    #[test]
    fn locks_are_acquired_released_and_contended() {
        let conn = conn();
        let keys = vec!["target:t1".to_string()];
        // First acquisition succeeds.
        assert_eq!(
            acquire_action_locks(
                &conn,
                "a1",
                &keys,
                "owner",
                "2030-01-01T00:00:00Z",
                "2024-01-01T00:00:00Z"
            )
            .unwrap(),
            Ok(())
        );
        // A second action is refused while the lease is still in the future.
        let refused = acquire_action_locks(
            &conn,
            "a2",
            &keys,
            "owner",
            "2030-01-01T00:00:00Z",
            "2024-01-01T00:00:00Z",
        )
        .unwrap();
        assert_eq!(
            refused,
            Err("resource-locked:target:t1:held-by:a1:until:2030-01-01T00:00:00Z".to_string())
        );
        // Re-acquiring for the same action is allowed and refreshes the lease.
        assert_eq!(
            acquire_action_locks(
                &conn,
                "a1",
                &keys,
                "owner2",
                "2031-01-01T00:00:00Z",
                "2024-06-01T00:00:00Z"
            )
            .unwrap(),
            Ok(())
        );
        // Releasing by action id drops only that action's locks.
        release_action_locks(&conn, "a1").unwrap();
        let remaining: i64 = conn
            .query_row("SELECT COUNT(*) FROM resilience_resource_locks", [], |r| {
                r.get(0)
            })
            .unwrap();
        assert_eq!(remaining, 0);
        // A released key can be taken by another action.
        assert_eq!(
            acquire_action_locks(
                &conn,
                "a2",
                &keys,
                "owner",
                "2032-01-01T00:00:00Z",
                "2024-07-01T00:00:00Z"
            )
            .unwrap(),
            Ok(())
        );
    }

    /// An expired lease is not contention: the new action takes the lock.
    #[test]
    fn an_expired_lease_does_not_block() {
        let conn = conn();
        let keys = vec!["target:expired".to_string()];
        assert_eq!(
            acquire_action_locks(
                &conn,
                "old",
                &keys,
                "owner",
                "2020-01-01T00:00:00Z",
                "2019-01-01T00:00:00Z",
            )
            .unwrap(),
            Ok(())
        );
        assert_eq!(
            acquire_action_locks(
                &conn,
                "new",
                &keys,
                "owner",
                "2030-01-01T00:00:00Z",
                "2024-01-01T00:00:00Z"
            )
            .unwrap(),
            Ok(())
        );
        let holder: String = conn
            .query_row(
                "SELECT action_id FROM resilience_resource_locks WHERE lock_key = ?",
                ["target:expired"],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(holder, "new");
    }

    /// `check_action_freshness` is the TOCTOU gate. The two branches that need
    /// no target registry are covered here; the rebalance branch is left to the
    /// route tests because it reads the registered-target store.
    #[test]
    fn action_freshness_matches_the_oracle_gate() {
        // A repair job needs an open replica risk for its own policy.
        let cleared = check_action_freshness(
            &json!({"type": "CREATE_REPAIR_JOB", "parameters": {"policyId": "p1"}}),
            &json!({"risks": []}),
        )
        .unwrap_err();
        assert_eq!(cleared, (true, "replica-risk-already-cleared".to_string()));
        // Any other policy's risk does not count.
        let other_policy = check_action_freshness(
            &json!({"type": "CREATE_REPAIR_JOB", "parameters": {"policyId": "p1"}}),
            &json!({"risks": [{"policyId": "p2", "type": "REPLICA_LAG", "severity": "critical"}]}),
        )
        .unwrap_err();
        assert_eq!(
            other_policy,
            (true, "replica-risk-already-cleared".to_string())
        );
        // A healthy or low severity is not an open risk either.
        for severity in ["healthy", "low", "LOW"] {
            let err = check_action_freshness(
                &json!({"type": "CREATE_REPAIR_JOB", "parameters": {"policyId": "p1"}}),
                &json!({"risks": [{"policyId": "p1", "type": "REPLICA_LAG", "severity": severity}]}),
            )
            .unwrap_err();
            assert_eq!(
                err,
                (true, "replica-risk-already-cleared".to_string()),
                "{severity}"
            );
        }
        // An open risk of either accepted type passes the gate.
        for risk_type in ["REPLICA_LAG", "FAILURE_DOMAIN_VIOLATION"] {
            assert!(check_action_freshness(
                &json!({"type": "CREATE_REPAIR_JOB", "parameters": {"policyId": "p1"}}),
                &json!({"risks": [{"policyId": "p1", "type": risk_type, "severity": "critical"}]}),
            )
            .is_ok());
        }
        // A DR drill needs an open DR_STALENESS risk.
        let no_dr =
            check_action_freshness(&json!({"type": "START_DR_DRILL"}), &json!({"risks": []}))
                .unwrap_err();
        assert_eq!(no_dr, (true, "dr-staleness-already-cleared".to_string()));
        let healthy_dr = check_action_freshness(
            &json!({"type": "START_DR_DRILL"}),
            &json!({"risks": [{"type": "DR_STALENESS", "severity": "low"}]}),
        )
        .unwrap_err();
        assert_eq!(
            healthy_dr,
            (true, "dr-staleness-already-cleared".to_string())
        );
        assert!(
            check_action_freshness(
                &json!({"type": "START_DR_DRILL"}),
                &json!({"risks": [{"type": "DR_STALENESS", "severity": "warning"}]}),
            )
            .is_ok()
        );
        // Any other action type has no gate.
        assert!(check_action_freshness(&json!({"type": "OTHER"}), &json!({"risks": []})).is_ok());
        // A missing `risks` key is an empty list, not an error.
        assert!(check_action_freshness(&json!({"type": "OTHER"}), &json!({})).is_ok());
    }

    /// `ensure_locks_schema` is idempotent, so repeated calls are safe.
    #[test]
    fn locks_schema_is_idempotent() {
        let conn = conn();
        ensure_locks_schema(&conn).unwrap();
        ensure_locks_schema(&conn).unwrap();
        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'resilience_resource_locks'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 1);
    }
}
