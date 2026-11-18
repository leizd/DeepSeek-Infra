//! Read-only `POST /api/workspace/disaster-recovery/plan`
//! (`backup_recovery_planner.plan_recovery`).
//!
//! Resolves a logical recovery point and ranks compatible backup targets
//! strictly from persisted `.backup-policies/`, `.backup-targets/`,
//! `.backup-control/control.sqlite3`, and `.backup-dr/evidence.sqlite3` state
//! without touching remote storage or creating directories on read.

use std::collections::{HashMap, HashSet};
use std::path::Path;

use axum::Json;
use axum::Router;
use axum::body::Bytes;
use axum::http::{HeaderMap, Method};
use axum::response::Response;
use axum::routing::any;
use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::core_utils::python_truthy;
use deepseek_policy::python_json::value_str;
use rusqlite::{Connection, OptionalExtension};
use serde_json::{Map, Value, json};

use crate::backup_catalog_routes::{
    list_registered_targets, open_readonly_sqlite, utc_iso_from_unix,
};
use crate::backup_dr_slo_routes::now_unix_secs;
use crate::backup_dr_status_routes::{
    classify_and_calibrate_rto, latest_drill_outcome, latest_recoverable_point,
    latest_scrub_outcome, list_logical_recovery_copies, table_columns, target_evidence_row,
};
use crate::route_support::{self, unhandled_error};

const MAX_PLAN_BODY_BYTES: i64 = 32_000;
const PLAN_SCHEMA_VERSION: i64 = 1;
const DEFAULT_MAX_FAILOVERS: i64 = 3;

#[derive(Clone)]
struct ResolvedRecoveryPoint {
    backup_id: String,
    object_set_digest: String,
    committed_at: String,
    chain_length: i64,
    logical_bytes: i64,
    storage_protocol: String,
}

#[derive(Clone)]
struct PolicyRecoveryPoint {
    target_id: String,
    policy_id: String,
    backup_id: String,
    committed_at: String,
    chain_digest: String,
    chain_length: i64,
    logical_bytes: i64,
    recoverable: bool,
    storage_protocol: String,
    metadata: Value,
}

struct ScoredCandidate {
    rank_key: (i64, i64, i64, i64, i64, i64, String),
    payload: Value,
    selection_reasons: Vec<String>,
    rto_estimate: Value,
}

pub fn router() -> Router {
    Router::new().route("/api/workspace/disaster-recovery/plan", any(dispatch))
}

async fn dispatch(method: Method, headers: HeaderMap, body: Bytes) -> Response {
    if method != Method::POST {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    let payload = match route_support::read_json_object(&headers, &body, MAX_PLAN_BODY_BYTES) {
        Ok(map) => map,
        Err(err) => return route_support::app_error_response(err),
    };
    let joined = tokio::task::spawn_blocking(move || build_plan_body(&payload)).await;
    match joined {
        Ok(Ok(val)) => Json(val).into_response(),
        Ok(Err(err)) => route_support::app_error_response(err),
        Err(_) => route_support::app_error_response(unhandled_error()),
    }
}

fn truthy_str(value: Option<&Value>) -> String {
    match value {
        Some(val) if python_truthy(val) => value_str(val),
        _ => String::new(),
    }
}

fn non_empty_opt_str(value: Option<&Value>) -> Option<String> {
    let text = truthy_str(value);
    if text.is_empty() { None } else { Some(text) }
}

fn is_safe_policy_id(policy_id: &str) -> bool {
    let bytes = policy_id.as_bytes();
    if bytes.is_empty() || bytes.len() > 64 {
        return false;
    }
    let first = bytes[0];
    if !(first.is_ascii_lowercase() || first.is_ascii_digit()) {
        return false;
    }
    bytes[1..]
        .iter()
        .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || matches!(b, b'.' | b'_' | b'-'))
}

fn lookup_policy_or_fallback(root: &Path, policy_id: &str) -> Result<Value, AppError> {
    let fallback = || {
        json!({
            "policyId": policy_id,
            "targetId": "managed-local",
            "replication": {"enabled": false},
        })
    };
    if !is_safe_policy_id(policy_id) {
        return Ok(fallback());
    }

    let control_db = root.join(".backup-control").join("control.sqlite3");
    if let Some(conn) = open_readonly_sqlite(&control_db)? {
        if !table_columns(&conn, "control_policies")?.is_empty() {
            let raw: Option<String> = conn
                .query_row(
                    "SELECT payload_json FROM control_policies WHERE policy_id = ?",
                    [policy_id],
                    |row| row.get(0),
                )
                .optional()
                .map_err(|_| unhandled_error())?;
            if let Some(text) = raw {
                if let Ok(parsed) = serde_json::from_str::<Value>(&text) {
                    let has_id = parsed
                        .as_object()
                        .and_then(|o| o.get("policyId"))
                        .is_some_and(python_truthy);
                    if has_id {
                        return Ok(parsed);
                    }
                }
            }
        }
    }

    let path = root
        .join(".backup-policies")
        .join(format!("{policy_id}.json"));
    if path.is_file() {
        if let Ok(text) = std::fs::read_to_string(&path) {
            if let Ok(parsed) = serde_json::from_str::<Value>(&text) {
                let has_id = parsed
                    .as_object()
                    .and_then(|o| o.get("policyId"))
                    .is_some_and(python_truthy);
                if has_id {
                    return Ok(parsed);
                }
            }
        }
    }

    Ok(fallback())
}

fn list_policy_recovery_points(
    dr_conn: Option<&Connection>,
    target_id: Option<&str>,
    policy_id: &str,
    limit: i64,
) -> Result<Vec<PolicyRecoveryPoint>, AppError> {
    let Some(conn) = dr_conn else {
        return Ok(Vec::new());
    };
    let cols = table_columns(conn, "recovery_points")?;
    if cols.is_empty() {
        return Ok(Vec::new());
    }
    let cd_col = if cols.contains("chain_digest") {
        "chain_digest"
    } else {
        "NULL"
    };
    let cl_col = if cols.contains("chain_length") {
        "chain_length"
    } else {
        "1"
    };
    let lb_col = if cols.contains("logical_bytes") {
        "logical_bytes"
    } else {
        "0"
    };
    let sp_col = if cols.contains("storage_protocol") {
        "storage_protocol"
    } else {
        "NULL"
    };
    let meta_col = if cols.contains("metadata_json") {
        "metadata_json"
    } else {
        "NULL"
    };
    let mut clauses = vec!["policy_id = ?"];
    let mut params = vec![policy_id.to_string()];
    if let Some(tid) = target_id {
        clauses.push("target_id = ?");
        params.push(tid.to_string());
    }
    let sql = format!(
        "SELECT target_id, policy_id, backup_id, committed_at, {cd_col}, {cl_col}, {lb_col}, \
         recoverable, {sp_col}, {meta_col} \
         FROM recovery_points WHERE {} ORDER BY committed_at DESC LIMIT ?",
        clauses.join(" AND ")
    );
    let mut stmt = conn.prepare(&sql).map_err(|_| unhandled_error())?;
    let mut dyn_params: Vec<&dyn rusqlite::ToSql> =
        params.iter().map(|s| s as &dyn rusqlite::ToSql).collect();
    dyn_params.push(&limit);
    let rows = stmt
        .query_map(dyn_params.as_slice(), |row| {
            let meta_raw: Option<String> = row.get(9)?;
            let metadata = meta_raw
                .as_deref()
                .and_then(|s| serde_json::from_str::<Value>(s).ok())
                .filter(|v| v.is_object())
                .unwrap_or_else(|| json!({}));
            Ok(PolicyRecoveryPoint {
                target_id: row.get(0)?,
                policy_id: row.get(1)?,
                backup_id: row.get(2)?,
                committed_at: row.get(3)?,
                chain_digest: row.get::<_, Option<String>>(4)?.unwrap_or_default(),
                chain_length: row.get::<_, Option<i64>>(5)?.unwrap_or(1),
                logical_bytes: row.get::<_, Option<i64>>(6)?.unwrap_or(0),
                recoverable: row.get::<_, i64>(7)? != 0,
                storage_protocol: row.get::<_, Option<String>>(8)?.unwrap_or_default(),
                metadata,
            })
        })
        .map_err(|_| unhandled_error())?;
    let mut out = Vec::new();
    for row in rows {
        out.push(row.map_err(|_| unhandled_error())?);
    }
    Ok(out)
}

fn resolve_point_with_backup_id(
    dr_conn: Option<&Connection>,
    policy_id: &str,
    backup_id: &str,
) -> Result<ResolvedRecoveryPoint, AppError> {
    let points = list_policy_recovery_points(dr_conn, None, policy_id, 500)?;
    if let Some(point) = points
        .into_iter()
        .find(|p| p.backup_id == backup_id && p.recoverable)
    {
        let meta_osd = truthy_str(point.metadata.get("objectSetDigest"));
        let object_set_digest = if !meta_osd.is_empty() {
            meta_osd
        } else {
            point.chain_digest
        };
        let chain_length = if point.chain_length == 0 {
            1
        } else {
            point.chain_length
        };
        let storage_protocol = if point.storage_protocol.is_empty() {
            "object-set-v1".to_string()
        } else {
            point.storage_protocol
        };
        return Ok(ResolvedRecoveryPoint {
            backup_id: backup_id.to_string(),
            object_set_digest,
            committed_at: point.committed_at,
            chain_length,
            logical_bytes: point.logical_bytes,
            storage_protocol,
        });
    }

    let copies = list_logical_recovery_copies(dr_conn, Some(policy_id), Some(backup_id), None, 50)?;
    let Some(copy_match) = copies.into_iter().find(|c| {
        c.get("backupId").and_then(Value::as_str) == Some(backup_id)
            && c.get("recoverable").is_some_and(python_truthy)
    }) else {
        return Err(AppError {
            message: "No authenticated recoverable point for backupId under policy".to_string(),
            code: codes::NOT_FOUND,
            status: 404,
        });
    };
    let meta = copy_match.get("metadata");
    let logical_bytes = meta
        .and_then(|m| m.get("logicalBytes"))
        .filter(|v| python_truthy(v))
        .or_else(|| {
            meta.and_then(|m| m.get("size"))
                .filter(|v| python_truthy(v))
        })
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let storage_proto = meta
        .and_then(|m| m.get("storageProtocol"))
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_else(|| "object-set-v1".to_string());
    Ok(ResolvedRecoveryPoint {
        backup_id: backup_id.to_string(),
        object_set_digest: truthy_str(copy_match.get("objectSetDigest")),
        committed_at: truthy_str(copy_match.get("committedAt")),
        chain_length: 1,
        logical_bytes,
        storage_protocol: storage_proto,
    })
}

fn resolve_latest_policy_point(
    dr_conn: Option<&Connection>,
    primary_target: &str,
    policy_id: &str,
    now_iso: &str,
) -> Result<ResolvedRecoveryPoint, AppError> {
    if let Some(latest) = latest_recoverable_point(dr_conn, primary_target, policy_id, now_iso)? {
        let backup_id = truthy_str(latest.get("backupId"));
        let committed_at = truthy_str(latest.get("committedAt"));
        let chain_length = latest
            .get("chainLength")
            .filter(|v| python_truthy(v))
            .and_then(Value::as_i64)
            .unwrap_or(1);
        let logical_bytes = latest
            .get("logicalBytes")
            .and_then(Value::as_i64)
            .unwrap_or(0);
        let storage_protocol = non_empty_opt_str(latest.get("storageProtocol"))
            .unwrap_or_else(|| "object-set-v1".to_string());
        return Ok(ResolvedRecoveryPoint {
            backup_id,
            object_set_digest: String::new(),
            committed_at,
            chain_length,
            logical_bytes,
            storage_protocol,
        });
    }

    let copies = list_logical_recovery_copies(dr_conn, Some(policy_id), None, None, 50)?;
    let Some(head) = copies
        .into_iter()
        .find(|c| c.get("recoverable").is_some_and(python_truthy))
    else {
        return Err(AppError {
            message: "No policy recovery point available".to_string(),
            code: codes::NOT_FOUND,
            status: 404,
        });
    };
    Ok(ResolvedRecoveryPoint {
        backup_id: truthy_str(head.get("backupId")),
        object_set_digest: truthy_str(head.get("objectSetDigest")),
        committed_at: truthy_str(head.get("committedAt")),
        chain_length: 1,
        logical_bytes: 0,
        storage_protocol: "object-set-v1".to_string(),
    })
}

fn is_target_broken(evidence: Option<&Value>) -> bool {
    let Some(ev) = evidence else {
        return false;
    };
    let status = ev.get("status").and_then(Value::as_str).unwrap_or("");
    let scheduled_ready = ev.get("scheduledReady").and_then(Value::as_bool);
    matches!(status, "error" | "failed" | "broken") || scheduled_ready == Some(false)
}

fn audit_failed(dr_conn: Option<&Connection>, target_id: &str) -> Result<bool, AppError> {
    let Some(conn) = dr_conn else {
        return Ok(false);
    };
    if table_columns(conn, "audit_evidence")?.is_empty() {
        return Ok(false);
    }
    let status: Option<String> = conn
        .query_row(
            "SELECT status FROM audit_evidence WHERE target_id = ? ORDER BY started_at DESC LIMIT 1",
            [target_id],
            |row| row.get(0),
        )
        .optional()
        .map_err(|_| unhandled_error())?;
    Ok(matches!(status.as_deref(), Some("failed" | "error")))
}

fn resolve_target_kind(target_kinds: &HashMap<String, String>, target_id: &str) -> String {
    if target_id == "managed-local" {
        return "managed-local".to_string();
    }
    target_kinds
        .get(target_id)
        .filter(|s| !s.is_empty())
        .cloned()
        .unwrap_or_else(|| "filesystem".to_string())
}

fn build_candidate_list(
    primary_target: &str,
    replica_targets: &[String],
    copies: &[Value],
) -> Vec<String> {
    let mut candidate_ids = Vec::new();
    let mut seen = HashSet::new();
    let mut push_unique = |tid: String| {
        if !tid.is_empty() && seen.insert(tid.clone()) {
            candidate_ids.push(tid);
        }
    };
    push_unique(primary_target.to_string());
    for tid in replica_targets {
        push_unique(tid.clone());
    }
    for copy in copies {
        push_unique(truthy_str(copy.get("targetId")));
    }
    candidate_ids
}

struct EvalContext<'a> {
    dr_conn: Option<&'a Connection>,
    policy_id: &'a str,
    primary_target: &'a str,
    requested_backup_id: Option<&'a str>,
    preferred_target_id: Option<&'a str>,
    point: &'a ResolvedRecoveryPoint,
    copies: &'a [Value],
    target_kinds: &'a HashMap<String, String>,
    now_iso: &'a str,
}

fn evaluate_candidate(
    ctx: &EvalContext<'_>,
    target_id: &str,
) -> Result<Result<ScoredCandidate, Value>, AppError> {
    let mut reasons = Vec::new();
    let mut rank_reasons = Vec::new();
    let matched_copy = ctx
        .copies
        .iter()
        .find(|c| c.get("targetId").and_then(Value::as_str) == Some(target_id));
    let copy_recoverable =
        matched_copy.is_some_and(|c| c.get("recoverable").is_some_and(python_truthy));
    if copy_recoverable {
        reasons.push("logical-copy-recoverable".to_string());
    } else {
        let pt = latest_recoverable_point(ctx.dr_conn, target_id, ctx.policy_id, ctx.now_iso)?;
        let pt_matches = pt.as_ref().is_some_and(|p| {
            ctx.requested_backup_id.is_none()
                || truthy_str(p.get("backupId")) == ctx.point.backup_id
        });
        if !pt_matches {
            let target_points =
                list_policy_recovery_points(ctx.dr_conn, Some(target_id), ctx.policy_id, 100)?;
            let has_row = target_points
                .iter()
                .any(|p| p.backup_id == ctx.point.backup_id && p.recoverable);
            if !has_row {
                return Ok(Err(json!({
                    "targetId": target_id,
                    "reasons": ["not-recoverable-on-target"],
                })));
            }
        }
        reasons.push("recoverable-on-target".to_string());
    }

    let tev = target_evidence_row(ctx.dr_conn, target_id)?;
    if is_target_broken(tev.as_ref()) {
        return Ok(Err(json!({
            "targetId": target_id,
            "reasons": ["target-known-broken"],
        })));
    }
    if audit_failed(ctx.dr_conn, target_id)? {
        return Ok(Err(json!({
            "targetId": target_id,
            "reasons": ["audit-known-failed"],
        })));
    }

    let kind = resolve_target_kind(ctx.target_kinds, target_id);
    reasons.push(format!("target-kind:{kind}"));
    reasons.push("credential-compatible".to_string());

    let is_healthy = tev
        .as_ref()
        .and_then(|v| v.get("status"))
        .and_then(Value::as_str)
        == Some("ok");
    if is_healthy {
        rank_reasons.push("target-health-ok".to_string());
    }
    let is_preferred = ctx.preferred_target_id == Some(target_id);
    if is_preferred {
        rank_reasons.push("explicit-preference".to_string());
    }
    let is_primary = target_id == ctx.primary_target;
    if is_primary {
        rank_reasons.push("primary-target".to_string());
    }

    let scrub = latest_scrub_outcome(ctx.dr_conn, target_id, ctx.policy_id, ctx.now_iso)?;
    let scrub_ok = scrub
        .as_ref()
        .and_then(|v| v.get("result"))
        .and_then(Value::as_str)
        == Some("success");
    if scrub_ok {
        rank_reasons.push("scrub-fresh".to_string());
    }

    let drill = latest_drill_outcome(ctx.dr_conn, target_id, ctx.policy_id, ctx.now_iso)?;
    let drill_ok = drill
        .as_ref()
        .and_then(|v| v.get("result"))
        .and_then(Value::as_str)
        == Some("success");
    if drill_ok {
        rank_reasons.push("drill-fresh".to_string());
    }

    let rto = classify_and_calibrate_rto(
        ctx.dr_conn,
        target_id,
        &kind,
        &json!({
            "logicalBytes": ctx.point.logical_bytes,
            "chainLength": ctx.point.chain_length,
            "storageProtocol": ctx.point.storage_protocol,
        }),
    )?;
    let rto_calibrated = rto.get("status").and_then(Value::as_str) == Some("calibrated");
    if rto_calibrated {
        rank_reasons.push("matching-rto-evidence".to_string());
    }

    let rto_seconds = rto
        .get("p50Seconds")
        .filter(|v| python_truthy(v))
        .or_else(|| rto.get("estimatedSeconds").filter(|v| python_truthy(v)))
        .and_then(Value::as_i64)
        .unwrap_or(999_999);
    let rank_key = (
        i64::from(!is_healthy),
        i64::from(!is_preferred),
        i64::from(!(is_primary || target_id == "managed-local")),
        i64::from(!drill_ok),
        i64::from(!scrub_ok),
        rto_seconds,
        target_id.to_string(),
    );
    let score = (if is_preferred { 100 } else { 0 })
        + (if is_primary { 40 } else { 0 })
        + (if is_healthy { 20 } else { 0 })
        + (if scrub_ok { 15 } else { 0 })
        + (if drill_ok { 15 } else { 0 })
        + (if rto_calibrated { 10 } else { 0 });

    let mut selection_reasons = rank_reasons.clone();
    selection_reasons.extend(reasons.iter().cloned());

    let candidate_json = json!({
        "targetId": target_id,
        "role": if is_primary { "primary" } else { "replica" },
        "kind": kind,
        "score": score,
        "rankKey": [
            rank_key.0,
            rank_key.1,
            rank_key.2,
            rank_key.3,
            rank_key.4,
            rank_key.5,
            rank_key.6,
        ],
        "filterReasons": reasons,
        "rankReasons": rank_reasons,
        "rtoEstimate": rto,
        "estimatedBytes": ctx.point.logical_bytes,
    });

    Ok(Ok(ScoredCandidate {
        rank_key,
        payload: candidate_json,
        selection_reasons,
        rto_estimate: rto,
    }))
}

pub(crate) fn build_plan_body(payload: &Map<String, Value>) -> Result<Value, AppError> {
    let policy_id = truthy_str(payload.get("policyId"));
    if policy_id.is_empty() {
        return Err(AppError {
            message: "policyId is required".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    }
    let requested_backup_id = non_empty_opt_str(payload.get("backupId"));
    let preferred_target_id = non_empty_opt_str(payload.get("preferredTargetId"));

    let root = crate::data_routes::workspace_root();
    let now_iso = utc_iso_from_unix(now_unix_secs());
    let policy = lookup_policy_or_fallback(&root, &policy_id)?;
    let primary_target =
        non_empty_opt_str(policy.get("targetId")).unwrap_or_else(|| "managed-local".to_string());

    let mut replica_targets = Vec::new();
    if let Some(repl) = policy.get("replication").and_then(Value::as_object) {
        if repl.get("enabled").is_some_and(python_truthy) {
            if let Some(items) = repl.get("targets").and_then(Value::as_array) {
                for entry in items {
                    if let Some(tid) = entry
                        .as_object()
                        .and_then(|obj| non_empty_opt_str(obj.get("targetId")))
                    {
                        replica_targets.push(tid);
                    }
                }
            }
        }
    }

    let dr_db = root.join(".backup-dr").join("evidence.sqlite3");
    let dr_conn = open_readonly_sqlite(&dr_db)?;

    let point = if let Some(bid) = requested_backup_id.as_deref() {
        resolve_point_with_backup_id(dr_conn.as_ref(), &policy_id, bid)?
    } else {
        resolve_latest_policy_point(dr_conn.as_ref(), &primary_target, &policy_id, &now_iso)?
    };

    let osd_filter = if point.object_set_digest.is_empty() {
        None
    } else {
        Some(point.object_set_digest.as_str())
    };
    let mut copies = list_logical_recovery_copies(
        dr_conn.as_ref(),
        Some(&policy_id),
        Some(&point.backup_id),
        osd_filter,
        100,
    )?;
    if copies.is_empty() {
        let all_pts = list_policy_recovery_points(dr_conn.as_ref(), None, &policy_id, 500)?;
        for p in all_pts {
            if p.backup_id == point.backup_id && p.recoverable {
                let meta_osd = p
                    .metadata
                    .as_object()
                    .and_then(|o| o.get("objectSetDigest"))
                    .filter(|v| !v.is_null())
                    .cloned()
                    .unwrap_or(Value::Null);
                let role = if p.target_id == primary_target {
                    "primary"
                } else {
                    "replica"
                };
                copies.push(json!({
                    "targetId": p.target_id,
                    "policyId": p.policy_id,
                    "backupId": p.backup_id,
                    "objectSetDigest": meta_osd,
                    "committedAt": p.committed_at,
                    "recoverable": true,
                    "role": role,
                }));
            }
        }
    }

    let targets = list_registered_targets()?;
    let target_kinds: HashMap<String, String> = targets
        .into_iter()
        .filter_map(|target| {
            let obj = target.as_object()?;
            let tid = truthy_str(obj.get("targetId"));
            if tid.is_empty() {
                return None;
            }
            let kind = obj
                .get("kind")
                .filter(|v| python_truthy(v))
                .map(|v| value_str(v).trim().to_ascii_lowercase())
                .unwrap_or_default();
            Some((tid, kind))
        })
        .collect();

    let candidate_ids = build_candidate_list(&primary_target, &replica_targets, &copies);
    let eval_ctx = EvalContext {
        dr_conn: dr_conn.as_ref(),
        policy_id: &policy_id,
        primary_target: &primary_target,
        requested_backup_id: requested_backup_id.as_deref(),
        preferred_target_id: preferred_target_id.as_deref(),
        point: &point,
        copies: &copies,
        target_kinds: &target_kinds,
        now_iso: &now_iso,
    };

    let mut scored = Vec::new();
    let mut rejected = Vec::new();
    for tid in candidate_ids {
        match evaluate_candidate(&eval_ctx, &tid)? {
            Ok(item) => scored.push(item),
            Err(rej) => rejected.push(rej),
        }
    }
    scored.sort_by(|a, b| a.rank_key.cmp(&b.rank_key));
    let Some(selected) = scored.first() else {
        return Err(AppError {
            message: "No compatible recovery target candidates".to_string(),
            code: codes::NOT_FOUND,
            status: 404,
        });
    };

    let selected_target_id = truthy_str(selected.payload.get("targetId"));
    let selection_reasons = selected.selection_reasons.clone();
    let matching_rto = selected.rto_estimate.clone();
    let ordered_candidates: Vec<Value> = scored.into_iter().map(|s| s.payload).collect();

    Ok(json!({
        "schemaVersion": PLAN_SCHEMA_VERSION,
        "policyId": policy_id,
        "logicalRecoveryPoint": {
            "backupId": point.backup_id,
            "policyId": policy_id,
            "objectSetDigest": if point.object_set_digest.is_empty() { Value::Null } else { Value::String(point.object_set_digest) },
            "committedAt": if point.committed_at.is_empty() { Value::Null } else { Value::String(point.committed_at) },
            "chainLength": point.chain_length,
            "logicalBytes": point.logical_bytes,
            "storageProtocol": point.storage_protocol,
        },
        "selectedTargetId": selected_target_id,
        "orderedCandidates": ordered_candidates,
        "rejectedCandidates": rejected,
        "selectionReasons": selection_reasons,
        "estimatedBytes": point.logical_bytes,
        "matchingRtoEvidence": matching_rto,
        "maxFailovers": DEFAULT_MAX_FAILOVERS,
        "plannedAt": now_iso,
        "source": "dr-evidence-ledger",
        "remoteIo": false,
    }))
}
