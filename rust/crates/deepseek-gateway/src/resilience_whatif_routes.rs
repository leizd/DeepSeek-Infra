//! Read-only side-effect-free fleet what-if simulation:
//! `POST /api/workspace/resilience/whatif` (Slice 7a).
//!
//! Binds hypothetical placement/capacity deltas to live authoritative server truth
//! (`.backup-control/control.sqlite3`, `.resilience-capacity/capacity.sqlite3`,
//! `.resilience-cost/cost.sqlite3`, `.backup-dr/evidence.sqlite3`, `.resilience-journal/journal.sqlite3`,
//! and registered filesystem targets) without creating directories or mutating state.

use std::collections::{BTreeSet, HashMap};
use std::path::{Path, PathBuf};

use axum::Router;
use axum::body::Bytes;
use axum::http::{HeaderMap, Method};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::core_utils::python_truthy;
use deepseek_policy::python_json::value_str;
use rusqlite::OptionalExtension;
use serde_json::{Map, Value, json};

use crate::backup_catalog_routes::open_readonly_sqlite;
use crate::backup_dr_status_routes::list_logical_recovery_copies;
use crate::resilience_read_routes::table_exists;
use crate::resilience_state_support::{
    build_fresh_state_bundle_readonly, capture_mutation_state, get_control_policy,
    latest_recovery_point, list_control_targets, now_unix_secs_and_iso, sha256_compact,
};
use crate::route_support;

const MAX_BODY_BYTES: i64 = 1_048_576;
const GIB: f64 = 1_073_741_824.0;

const ALLOWED_CANDIDATE_FIELDS: [&str; 15] = [
    "policyId",
    "targetId",
    "sourceTargetId",
    "destTargetId",
    "backupId",
    "operation",
    "committedCopiesDelta",
    "failureDomainsDelta",
    "storedBytes",
    "additionalStoredBytes",
    "replicationBytes",
    "egressBytes",
    "retrievalBytes",
    "requestCount",
    "forecastHorizonDays",
];

pub fn router() -> Router {
    Router::new().route("/api/workspace/resilience/whatif", any(dispatch))
}

async fn dispatch(method: Method, headers: HeaderMap, body: Bytes) -> Response {
    if method != Method::POST {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    let body_obj = match route_support::read_json_object(&headers, &body, MAX_BODY_BYTES) {
        Ok(obj) => obj,
        Err(err) => return route_support::app_error_response(err),
    };
    if body_obj.len() != 1 || !body_obj.contains_key("candidate") {
        return route_support::app_error_response(AppError::invalid_payload(
            "what-if accepts candidate only; present truth is server-controlled",
        ));
    }
    let Some(candidate) = body_obj
        .get("candidate")
        .and_then(Value::as_object)
        .cloned()
    else {
        return route_support::app_error_response(AppError::invalid_payload(
            "candidate is required",
        ));
    };

    let joined = tokio::task::spawn_blocking(move || {
        let root = crate::data_routes::workspace_root();
        execute_whatif(&root, &candidate)
    })
    .await;

    match joined {
        Ok(Ok(payload)) => axum::Json(payload).into_response(),
        Ok(Err(err)) => route_support::app_error_response(err),
        Err(_) => route_support::app_error_response(route_support::unhandled_error()),
    }
}

fn control_db_path(root: &Path) -> PathBuf {
    root.join(".backup-control").join("control.sqlite3")
}

fn dr_db_path(root: &Path) -> PathBuf {
    root.join(".backup-dr").join("evidence.sqlite3")
}

fn capacity_db_path(root: &Path) -> PathBuf {
    root.join(".resilience-capacity").join("capacity.sqlite3")
}

fn cost_db_path(root: &Path) -> PathBuf {
    root.join(".resilience-cost").join("cost.sqlite3")
}

fn unavailable_error(reason: impl Into<String>) -> AppError {
    AppError {
        message: reason.into(),
        code: codes::INVALID_REQUEST,
        status: 503,
    }
}

fn candidate_integer(
    candidate: &Map<String, Value>,
    field: &str,
    default: i64,
    allow_negative: bool,
) -> Result<i64, AppError> {
    let Some(val) = candidate.get(field) else {
        return Ok(default);
    };
    let parsed = if val.is_boolean() { None } else { val.as_i64() };
    let Some(num) = parsed else {
        return Err(AppError::invalid_payload(format!(
            "candidate.{field} must be an integer"
        )));
    };
    if !allow_negative && num < 0 {
        return Err(AppError::invalid_payload(format!(
            "candidate.{field} must be non-negative"
        )));
    }
    Ok(num)
}

fn validate_candidate(candidate: &Map<String, Value>) -> Result<Value, AppError> {
    if candidate.is_empty() {
        return Err(AppError::invalid_payload("candidate is required"));
    }
    let unknown: Vec<&str> = candidate
        .keys()
        .map(String::as_str)
        .filter(|k| !ALLOWED_CANDIDATE_FIELDS.contains(k))
        .collect();
    if !unknown.is_empty() {
        let mut sorted = unknown;
        sorted.sort_unstable();
        return Err(AppError::invalid_payload(format!(
            "caller-controlled present truth is forbidden: {}",
            sorted.join(", ")
        )));
    }
    let policy_id = candidate
        .get("policyId")
        .filter(|v| python_truthy(v))
        .map(|v| value_str(v).trim().to_string())
        .unwrap_or_default();
    if policy_id.is_empty() {
        return Err(AppError::invalid_payload("candidate.policyId is required"));
    }
    let target_id = candidate
        .get("targetId")
        .filter(|v| python_truthy(v))
        .or_else(|| candidate.get("destTargetId").filter(|v| python_truthy(v)))
        .map(|v| value_str(v).trim().to_string())
        .unwrap_or_default();
    if target_id.is_empty() {
        return Err(AppError::invalid_payload(
            "candidate.targetId or candidate.destTargetId is required",
        ));
    }
    let operation = candidate
        .get("operation")
        .filter(|v| python_truthy(v))
        .map(|v| value_str(v).trim().to_uppercase())
        .unwrap_or_else(|| "KEEP".to_string());
    if !matches!(operation.as_str(), "KEEP" | "ADD_REPLICA" | "REBALANCE") {
        return Err(AppError::invalid_payload(
            "candidate.operation must be KEEP, ADD_REPLICA, or REBALANCE",
        ));
    }
    let horizon = candidate_integer(candidate, "forecastHorizonDays", 90, false)?;
    if horizon != 30 && horizon != 90 {
        return Err(AppError::invalid_payload(
            "candidate.forecastHorizonDays must be 30 or 90",
        ));
    }
    for field in [
        "storedBytes",
        "additionalStoredBytes",
        "replicationBytes",
        "egressBytes",
        "retrievalBytes",
        "requestCount",
    ] {
        candidate_integer(candidate, field, 0, false)?;
    }
    candidate_integer(candidate, "committedCopiesDelta", 0, true)?;
    candidate_integer(candidate, "failureDomainsDelta", 0, true)?;

    let mut out = candidate.clone();
    out.insert("policyId".to_string(), Value::String(policy_id));
    out.insert("targetId".to_string(), Value::String(target_id));
    out.insert("operation".to_string(), Value::String(operation));
    out.insert("forecastHorizonDays".to_string(), json!(horizon));
    Ok(Value::Object(out))
}

fn read_baseline_readonly(root: &Path, policy: &Value) -> Result<Value, AppError> {
    let policy_id = policy
        .get("policyId")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let replication = policy.get("replication").and_then(Value::as_object);
    let placement = policy.get("placement").and_then(Value::as_object);
    let dr_conn = open_readonly_sqlite(&dr_db_path(root))
        .map_err(|e| unavailable_error(format!("BASELINE_TRUTH_UNAVAILABLE: {}", e.message)))?;
    let control_conn = open_readonly_sqlite(&control_db_path(root))
        .map_err(|e| unavailable_error(format!("BASELINE_TRUTH_UNAVAILABLE: {}", e.message)))?;

    let latest = latest_recovery_point(dr_conn.as_ref(), policy_id)
        .map_err(|e| unavailable_error(format!("BASELINE_TRUTH_UNAVAILABLE: {}", e.message)))?;
    let backup_id = latest
        .as_ref()
        .and_then(|pt| pt.get("backupId"))
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let copies = if backup_id.is_empty() {
        Vec::new()
    } else {
        list_logical_recovery_copies(
            dr_conn.as_ref(),
            Some(policy_id),
            Some(&backup_id),
            None,
            1000,
        )
        .map_err(|e| unavailable_error(format!("BASELINE_TRUTH_UNAVAILABLE: {}", e.message)))?
    };
    let target_list = list_control_targets(control_conn.as_ref())
        .map_err(|e| unavailable_error(format!("BASELINE_TRUTH_UNAVAILABLE: {}", e.message)))?;
    let targets: HashMap<String, Value> = target_list
        .into_iter()
        .filter_map(|t| {
            let id = t
                .get("targetId")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())?
                .to_string();
            Some((id, t))
        })
        .collect();

    let committed: Vec<&Value> = copies
        .iter()
        .filter(|item| {
            item.get("recoverable") == Some(&Value::Bool(true))
                && item
                    .get("state")
                    .filter(|v| python_truthy(v))
                    .and_then(Value::as_str)
                    .unwrap_or("healthy")
                    == "healthy"
        })
        .collect();

    let mut domains = BTreeSet::new();
    for copy in &committed {
        let tid = copy
            .get("targetId")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        let Some(target) = targets.get(&tid) else {
            return Err(unavailable_error(format!(
                "BASELINE_TARGET_UNAVAILABLE: {tid}"
            )));
        };
        let domain = target
            .get("failureDomain")
            .filter(|v| python_truthy(v))
            .map(|v| value_str(v).trim().to_string())
            .unwrap_or_default();
        if domain.is_empty() {
            return Err(unavailable_error(format!(
                "BASELINE_FAILURE_DOMAIN_UNAVAILABLE: {tid}"
            )));
        }
        domains.insert(domain);
    }

    let policy_rev = policy
        .get("policyRevision")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let min_copies = replication
        .and_then(|r| r.get("minCommittedCopies"))
        .and_then(Value::as_i64)
        .filter(|v| *v != 0)
        .unwrap_or(1);
    let min_fds = replication
        .and_then(|r| r.get("minFailureDomains"))
        .and_then(Value::as_i64)
        .filter(|v| *v != 0)
        .unwrap_or(1);
    let headroom = placement
        .and_then(|p| p.get("minFreeBytes"))
        .and_then(Value::as_i64)
        .unwrap_or(0);

    Ok(json!({
        "policyId": policy_id,
        "backupId": if backup_id.is_empty() { Value::Null } else { Value::String(backup_id) },
        "policyRevision": policy_rev,
        "minCommittedCopies": min_copies,
        "minFailureDomains": min_fds,
        "committedCopies": committed.len(),
        "failureDomains": domains.len(),
        "failureDomainIds": domains.into_iter().collect::<Vec<_>>(),
        "forecastSafetyHeadroomBytes": headroom,
    }))
}

fn current_forecast_readonly(root: &Path, target_id: &str, horizon_days: i64) -> Option<Value> {
    let conn = open_readonly_sqlite(&capacity_db_path(root)).ok()??;
    if !table_exists(&conn, "resilience_capacity_observations").unwrap_or(false)
        || !table_exists(&conn, "resilience_forecast_records").unwrap_or(false)
    {
        return None;
    }
    let series: Option<(String, String)> = conn
        .query_row(
            "SELECT target_incarnation, capacity_revision \
             FROM resilience_capacity_observations \
             WHERE target_id = ? \
             ORDER BY observed_at DESC, observation_key DESC \
             LIMIT 1",
            [target_id],
            |row| {
                let inc: Option<String> = row.get(0)?;
                let rev: Option<String> = row.get(1)?;
                Ok((
                    inc.filter(|s| !s.is_empty())
                        .unwrap_or_else(|| format!("legacy:{target_id}")),
                    rev.filter(|s| !s.is_empty())
                        .unwrap_or_else(|| "legacy".to_string()),
                ))
            },
        )
        .optional()
        .ok()
        .flatten();
    let (incarnation, revision) = series?;
    let h = horizon_days.max(1);
    conn.query_row(
        "SELECT forecast_id, target_id, target_incarnation, capacity_revision, horizon_days, \
                forecasted_at, evaluation_due_at, p50_free_bytes, p90_free_bytes, \
                forecast_digest, observation_set_digest, forecast_json, status, \
                actual_observation_key, backtest_key, created_at, updated_at \
         FROM resilience_forecast_records \
         WHERE target_id = ? AND target_incarnation = ? AND capacity_revision = ? \
           AND horizon_days = ? AND status IN ('ACTIVE', 'DUE') \
         ORDER BY forecasted_at DESC, forecast_id DESC \
         LIMIT 1",
        rusqlite::params![target_id, incarnation, revision, h],
        |row| {
            let fj_raw: String = row.get(11)?;
            let fj = serde_json::from_str::<Value>(&fj_raw)
                .ok()
                .filter(Value::is_object)
                .unwrap_or_else(|| json!({}));
            Ok(json!({
                "forecastId": row.get::<_, String>(0)?,
                "targetId": row.get::<_, String>(1)?,
                "targetIncarnation": row.get::<_, String>(2)?,
                "capacityRevision": row.get::<_, String>(3)?,
                "horizonDays": row.get::<_, i64>(4)?,
                "forecastedAt": row.get::<_, String>(5)?,
                "evaluationDueAt": row.get::<_, String>(6)?,
                "p50FreeBytes": row.get::<_, i64>(7)?,
                "p90FreeBytes": row.get::<_, i64>(8)?,
                "forecastDigest": row.get::<_, String>(9)?,
                "capacityObservationSetDigest": row.get::<_, String>(10)?,
                "status": row.get::<_, String>(12)?,
                "actualObservationKey": row.get::<_, Option<String>>(13)?.unwrap_or_default(),
                "backtestKey": row.get::<_, Option<String>>(14)?.unwrap_or_default(),
                "forecast": fj,
                "createdAt": row.get::<_, String>(15)?,
                "updatedAt": row.get::<_, String>(16)?,
            }))
        },
    )
    .optional()
    .ok()
    .flatten()
}

fn price_catalog_readonly(root: &Path) -> Option<Value> {
    let conn = open_readonly_sqlite(&cost_db_path(root)).ok()??;
    if !table_exists(&conn, "resilience_price_catalogs").unwrap_or(false) {
        return None;
    }
    let row = conn
        .query_row(
            "SELECT catalog_json, catalog_digest \
             FROM resilience_price_catalogs \
             ORDER BY price_catalog_version DESC \
             LIMIT 1",
            [],
            |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)),
        )
        .optional()
        .ok()
        .flatten()?;
    let mut parsed = serde_json::from_str::<Value>(&row.0).ok()?;
    let obj = parsed.as_object_mut()?;
    obj.insert("priceCatalogDigest".to_string(), Value::String(row.1));
    Some(parsed)
}

fn round6(value: f64) -> f64 {
    (value * 1_000_000.0).round() / 1_000_000.0
}

fn estimate_target_cost(candidate: &Value, catalog: Option<&Value>) -> Value {
    let target_id = candidate
        .get("targetId")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let catalog_digest = catalog
        .and_then(|c| c.get("priceCatalogDigest"))
        .cloned()
        .unwrap_or(Value::Null);
    let unknown = |dig: Value| {
        json!({
            "targetId": target_id,
            "status": "UNKNOWN_COST",
            "storage": Value::Null,
            "replicationTransfer": Value::Null,
            "egress": Value::Null,
            "retrieval": Value::Null,
            "requestCost": Value::Null,
            "monthlyCost": Value::Null,
            "priceCatalogDigest": dig,
        })
    };
    let Some(cat) = catalog else {
        return unknown(Value::Null);
    };
    let Some(prices) = cat
        .get("targets")
        .and_then(Value::as_object)
        .and_then(|m| m.get(target_id))
        .and_then(Value::as_object)
    else {
        return unknown(catalog_digest);
    };
    let Some(storage_rate) = prices.get("storagePerGiBMonth").and_then(Value::as_f64) else {
        return unknown(catalog_digest);
    };
    let Some(egress_rate) = prices.get("egressPerGiB").and_then(Value::as_f64) else {
        return unknown(catalog_digest);
    };
    let request_rate = prices
        .get("requestCost")
        .and_then(Value::as_f64)
        .unwrap_or(0.0);
    let retrieval_rate = prices
        .get("retrievalPerGiB")
        .and_then(Value::as_f64)
        .unwrap_or(egress_rate);

    let gib = |field: &str| -> f64 {
        let bytes = candidate
            .get(field)
            .and_then(Value::as_i64)
            .unwrap_or(0)
            .max(0) as f64;
        bytes / GIB
    };
    let req_count = candidate
        .get("requestCount")
        .and_then(Value::as_i64)
        .unwrap_or(0) as f64;

    let storage = gib("storedBytes") * storage_rate;
    let replication = gib("replicationBytes") * egress_rate;
    let egress = gib("egressBytes") * egress_rate;
    let retrieval = gib("retrievalBytes") * retrieval_rate;
    let requests = req_count * request_rate;
    let monthly = storage + replication + egress + retrieval + requests;

    json!({
        "targetId": target_id,
        "status": "OK",
        "storage": round6(storage),
        "replicationTransfer": round6(replication),
        "egress": round6(egress),
        "retrieval": round6(retrieval),
        "requestCost": round6(requests),
        "monthlyCost": round6(monthly),
        "priceCatalogDigest": catalog_digest,
        "priceCatalogVersion": cat.get("priceCatalogVersion").cloned().unwrap_or(Value::Null),
    })
}

fn evaluate_candidate(candidate: &Value, baseline: &Value, catalog: Option<&Value>) -> Value {
    let mut violations = Vec::new();
    let min_copies = baseline
        .get("minCommittedCopies")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let min_domains = baseline
        .get("minFailureDomains")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let cand_copies = candidate
        .get("committedCopies")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let cand_domains = candidate
        .get("failureDomains")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    if cand_copies < min_copies {
        violations.push("MIN_COMMITTED_COPIES_REDUCED");
    }
    if cand_domains < min_domains {
        violations.push("MIN_FAILURE_DOMAINS_REDUCED");
    }
    let baseline_copies = baseline
        .get("committedCopies")
        .and_then(Value::as_i64)
        .filter(|v| *v != 0)
        .unwrap_or(min_copies);
    if cand_copies < baseline_copies {
        violations.push("DEGRADED_BASELINE_COPIES_REDUCED");
    }
    let baseline_domains = baseline
        .get("failureDomains")
        .and_then(Value::as_i64)
        .filter(|v| *v != 0)
        .unwrap_or(min_domains);
    if cand_domains < baseline_domains {
        violations.push("DEGRADED_BASELINE_DOMAINS_REDUCED");
    }
    let safety = baseline
        .get("forecastSafetyHeadroomBytes")
        .and_then(Value::as_i64);
    let forecast_free = candidate.get("forecastFreeBytes").and_then(Value::as_i64);
    if let (Some(s), Some(ff)) = (safety, forecast_free) {
        if ff < s {
            violations.push("FORECAST_SAFETY_HEADROOM_REDUCED");
        }
    }
    if candidate.get("breaksDrDependency") == Some(&Value::Bool(true)) {
        violations.push("ACTIVE_DR_DEPENDENCY_BROKEN");
    }
    if candidate.get("mutatesAuthority") == Some(&Value::Bool(true)) {
        violations.push("AUTHORITY_MUTATION_FORBIDDEN");
    }

    let cost = estimate_target_cost(candidate, catalog);
    let accepted =
        violations.is_empty() && cost.get("status").and_then(Value::as_str) == Some("OK");
    let score = if accepted {
        let cap_risk = if forecast_free.unwrap_or(0) > safety.unwrap_or(0) {
            0.0
        } else {
            1.0
        };
        let transfer_cost = cost.get("egress").and_then(Value::as_f64).unwrap_or(0.0)
            + cost
                .get("replicationTransfer")
                .and_then(Value::as_f64)
                .unwrap_or(0.0);
        let domain_penalty = if cand_domains >= min_domains {
            0.0
        } else {
            10.0
        };
        let monthly = cost
            .get("monthlyCost")
            .and_then(Value::as_f64)
            .unwrap_or(0.0);
        json!(monthly + cap_risk + transfer_cost + domain_penalty)
    } else {
        Value::Null
    };

    json!({
        "candidate": candidate,
        "accepted": accepted,
        "violations": violations,
        "cost": cost,
        "score": score,
    })
}

fn build_optimizer_inputs(
    root: &Path,
    raw_candidate: &Map<String, Value>,
    now_secs: u64,
    now_iso: &str,
) -> Result<Value, AppError> {
    let proposed = validate_candidate(raw_candidate)?;
    let policy_id = proposed
        .get("policyId")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let target_id = proposed
        .get("targetId")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let horizon = proposed
        .get("forecastHorizonDays")
        .and_then(Value::as_i64)
        .unwrap_or(90);

    let control_conn = open_readonly_sqlite(&control_db_path(root))
        .map_err(|e| unavailable_error(format!("POLICY_TRUTH_UNAVAILABLE: {}", e.message)))?;
    let policy = get_control_policy(control_conn.as_ref(), policy_id)
        .map_err(|e| unavailable_error(format!("POLICY_TRUTH_UNAVAILABLE: {}", e.message)))?
        .filter(|v| v.as_object().is_some_and(|m| !m.is_empty()))
        .ok_or_else(|| unavailable_error("POLICY_TRUTH_UNAVAILABLE"))?;
    let baseline = read_baseline_readonly(root, &policy)?;

    let forecast_record = current_forecast_readonly(root, target_id, horizon)
        .ok_or_else(|| unavailable_error("FORECAST_REGISTRY_UNAVAILABLE"))?;
    let status_ok = matches!(
        forecast_record.get("status").and_then(Value::as_str),
        Some("ACTIVE" | "DUE")
    );
    if !status_ok {
        return Err(unavailable_error("FORECAST_REGISTRY_UNAVAILABLE"));
    }
    let raw_fc = forecast_record.get("forecast").and_then(Value::as_object);
    let forecast = match raw_fc {
        Some(obj) if !obj.is_empty() => Value::Object(obj.clone()),
        _ => json!({
            "targetId": forecast_record.get("targetId").cloned().unwrap_or(Value::Null),
            "horizonDays": forecast_record.get("horizonDays").cloned().unwrap_or(Value::Null),
            "forecastStatus": "OK",
            "p50FreeBytes": forecast_record.get("p50FreeBytes").cloned().unwrap_or(Value::Null),
            "p90FreeBytes": forecast_record.get("p90FreeBytes").cloned().unwrap_or(Value::Null),
            "forecastDigest": forecast_record.get("forecastDigest").cloned().unwrap_or(Value::Null),
            "capacityObservationSetDigest": forecast_record.get("capacityObservationSetDigest").cloned().unwrap_or(Value::Null),
        }),
    };
    if forecast.get("forecastStatus").and_then(Value::as_str) != Some("OK")
        || forecast
            .get("forecastDigest")
            .filter(|v| python_truthy(v))
            .is_none_or(|v| value_str(v).is_empty())
    {
        return Err(unavailable_error("FORECAST_REGISTRY_UNAVAILABLE"));
    }

    let catalog = price_catalog_readonly(root)
        .ok_or_else(|| unavailable_error("PRICE_CATALOG_UNAVAILABLE"))?;
    let cat_dig_ok = catalog
        .get("priceCatalogDigest")
        .filter(|v| python_truthy(v))
        .is_some_and(|v| !value_str(v).is_empty());
    let Some(cat_targets) = catalog.get("targets").and_then(Value::as_object) else {
        return Err(unavailable_error("PRICE_CATALOG_UNAVAILABLE"));
    };
    if !cat_dig_ok {
        return Err(unavailable_error("PRICE_CATALOG_UNAVAILABLE"));
    }
    if !cat_targets.get(target_id).is_some_and(Value::is_object) {
        return Err(unavailable_error("TARGET_PRICE_UNAVAILABLE"));
    }

    let op = proposed
        .get("operation")
        .and_then(Value::as_str)
        .unwrap_or("KEEP");
    let action_type = match op {
        "ADD_REPLICA" => "CREATE_REPAIR_JOB",
        "REBALANCE" => "CREATE_REBALANCE_JOB",
        _ => "NOOP",
    };
    let backup_id = proposed
        .get("backupId")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();
    let source_target = proposed
        .get("sourceTargetId")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();
    let dest_target = proposed
        .get("destTargetId")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_else(|| target_id.to_string());
    let candidate_action = json!({
        "actionId": "whatif-candidate",
        "type": action_type,
        "policyId": policy_id,
        "backupId": backup_id,
        "parameters": {
            "policyId": policy_id,
            "backupId": backup_id,
            "targetId": target_id,
            "sourceTargetId": source_target,
            "destTargetId": dest_target,
        },
    });

    let fresh = build_fresh_state_bundle_readonly(root, &[candidate_action], now_secs, now_iso)
        .map_err(unavailable_error)?;
    if fresh
        .get("freshStateBundleDigest")
        .filter(|v| python_truthy(v))
        .is_none()
    {
        return Err(unavailable_error("FRESH_STATE_BUNDLE_UNAVAILABLE"));
    }

    let base_copies = baseline
        .get("committedCopies")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let base_domains = baseline
        .get("failureDomains")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let copies_delta = proposed
        .get("committedCopiesDelta")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let domains_delta = proposed
        .get("failureDomainsDelta")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let p90_free = forecast
        .get("p90FreeBytes")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let add_stored = proposed
        .get("additionalStoredBytes")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let breaks_dr =
        fresh.get("blastSimulation").and_then(|b| b.get("passed")) != Some(&Value::Bool(true));

    let evaluated_candidate = json!({
        "policyId": policy_id,
        "targetId": target_id,
        "operation": op,
        "committedCopies": (base_copies + copies_delta).max(0),
        "failureDomains": (base_domains + domains_delta).max(0),
        "storedBytes": proposed.get("storedBytes").and_then(Value::as_i64).unwrap_or(0),
        "replicationBytes": proposed.get("replicationBytes").and_then(Value::as_i64).unwrap_or(0),
        "egressBytes": proposed.get("egressBytes").and_then(Value::as_i64).unwrap_or(0),
        "retrievalBytes": proposed.get("retrievalBytes").and_then(Value::as_i64).unwrap_or(0),
        "requestCount": proposed.get("requestCount").and_then(Value::as_i64).unwrap_or(0),
        "forecastFreeBytes": (p90_free - add_stored).max(0),
        "breaksDrDependency": breaks_dr,
        "mutatesAuthority": false,
    });

    let authoritative_targets = list_control_targets(control_conn.as_ref())
        .map_err(|e| unavailable_error(format!("TARGET_TRUTH_UNAVAILABLE: {}", e.message)))?;
    let mut target_ids_set = BTreeSet::new();
    for t in &authoritative_targets {
        if let Some(id) = t
            .get("targetId")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
        {
            target_ids_set.insert(id.to_string());
        }
    }
    let capacity_snapshot = fresh
        .get("capacitySnapshot")
        .filter(|v| v.is_object())
        .cloned()
        .unwrap_or_else(|| json!({}));
    if let Some(cap_list) = capacity_snapshot.get("targets").and_then(Value::as_array) {
        for item in cap_list {
            if let Some(id) = item
                .get("targetId")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
            {
                target_ids_set.insert(id.to_string());
            }
        }
    }
    target_ids_set.insert(target_id.to_string());
    let target_ids: Vec<String> = target_ids_set.into_iter().collect();

    let observed_at = fresh
        .get("observedAt")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_else(|| now_iso.to_string());
    let mut result = json!({
        "candidate": evaluated_candidate,
        "hypotheticalDelta": proposed,
        "baseline": baseline,
        "observedSnapshot": fresh.get("riskSnapshot").cloned().unwrap_or(Value::Null),
        "forecast": forecast,
        "forecastRecord": forecast_record,
        "priceCatalog": catalog,
        "authorityHeadDigest": fresh.get("authorityHeadDigest").and_then(Value::as_str).unwrap_or_default(),
        "freshStateBundleDigest": fresh.get("freshStateBundleDigest").and_then(Value::as_str).unwrap_or_default(),
        "freshStateBundle": fresh,
        "runningEffects": fresh.get("runningEffects").cloned().unwrap_or_else(|| json!([])),
        "maintenanceWindows": fresh.get("maintenanceDecisions").cloned().unwrap_or_else(|| json!([])),
        "capacitySnapshot": capacity_snapshot,
        "targetMetadata": authoritative_targets,
        "budgets": fresh.get("budgets").cloned().unwrap_or(Value::Null),
        "blastSimulation": fresh.get("blastSimulation").cloned().unwrap_or(Value::Null),
        "targetIds": target_ids,
        "observedAt": observed_at,
    });
    let input_digest = sha256_compact(&result);
    result["optimizerInputDigest"] = Value::String(input_digest);
    Ok(result)
}

fn execute_whatif(root: &Path, raw_candidate: &Map<String, Value>) -> Result<Value, AppError> {
    let (now_secs, now_iso) = now_unix_secs_and_iso();
    let before = capture_mutation_state(root).map_err(unavailable_error)?;
    let inputs = build_optimizer_inputs(root, raw_candidate, now_secs, &now_iso)?;
    let after = capture_mutation_state(root).map_err(unavailable_error)?;

    let candidate = inputs
        .get("candidate")
        .cloned()
        .unwrap_or_else(|| json!({}));
    let baseline = inputs.get("baseline").cloned().unwrap_or_else(|| json!({}));
    let catalog = inputs.get("priceCatalog").filter(|v| v.is_object());
    let forecast = inputs.get("forecast").filter(|v| v.is_object());
    let observed = inputs.get("observedSnapshot").filter(|v| v.is_object());

    let evaluation = evaluate_candidate(&candidate, &baseline, catalog);
    let source_snapshot_digest = observed
        .and_then(|o| o.get("riskDigest").filter(|v| python_truthy(v)))
        .or_else(|| observed.and_then(|o| o.get("snapshotDigest").filter(|v| python_truthy(v))))
        .map(value_str)
        .unwrap_or_default();
    let authority_head_digest = inputs
        .get("authorityHeadDigest")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();
    let forecast_digest = forecast
        .and_then(|f| f.get("forecastDigest").filter(|v| python_truthy(v)))
        .map(value_str)
        .unwrap_or_default();
    let price_catalog_digest = catalog
        .and_then(|c| c.get("priceCatalogDigest").cloned())
        .unwrap_or(Value::Null);

    let accepted = evaluation.get("accepted") == Some(&Value::Bool(true));
    let (selected_val, rejected_val, plan_status) = if accepted {
        (evaluation.clone(), json!([]), "OK")
    } else {
        (
            Value::Null,
            json!([evaluation.clone()]),
            "NO_SAFE_CANDIDATE",
        )
    };
    let selected_cand = selected_val
        .get("candidate")
        .cloned()
        .unwrap_or(Value::Null);
    let candidate_plan_digest = sha256_compact(&json!({
        "selected": selected_cand,
        "baseline": baseline,
        "sourceSnapshotDigest": source_snapshot_digest,
        "authorityHeadDigest": authority_head_digest,
        "forecastDigest": forecast_digest,
        "priceCatalogDigest": price_catalog_digest,
    }));
    let candidate_plan = json!({
        "status": plan_status,
        "baseline": baseline,
        "selected": selected_val,
        "rejected": rejected_val,
        "sourceSnapshotDigest": source_snapshot_digest,
        "authorityHeadDigest": authority_head_digest,
        "forecastDigest": forecast_digest,
        "priceCatalogDigest": price_catalog_digest,
        "candidatePlanDigest": candidate_plan_digest,
    });

    let pre_digests = before
        .get("digests")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let post_digests = after
        .get("digests")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let mut all_domains: BTreeSet<String> = pre_digests.keys().cloned().collect();
    all_domains.extend(post_digests.keys().cloned());
    let changed_domains: Vec<String> = all_domains
        .into_iter()
        .filter(|d| pre_digests.get(d) != post_digests.get(d))
        .collect();
    let pre_state_digest = before
        .get("stateDigest")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let post_state_digest = after
        .get("stateDigest")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let state_unchanged = pre_state_digest == post_state_digest && changed_domains.is_empty();

    let simulation = json!({
        "attemptedWrites": [],
        "blockedWrites": [],
        "attemptedMutationCount": 0,
        "blockedMutationCount": 0,
        "preStateDigest": pre_state_digest,
        "postStateDigest": post_state_digest,
        "preStateDigests": Value::Object(pre_digests),
        "postStateDigests": Value::Object(post_digests),
        "storageInventoryBefore": before.get("storageInventory").cloned().unwrap_or_else(|| json!([])),
        "storageInventoryAfter": after.get("storageInventory").cloned().unwrap_or_else(|| json!([])),
        "stateUnchanged": state_unchanged,
        "changedDomains": changed_domains,
    });

    let status = if !state_unchanged {
        "SIMULATION_VIOLATION"
    } else if accepted {
        "OK"
    } else {
        "REJECTED"
    };
    let cost = evaluation.get("cost").cloned().unwrap_or_else(|| json!({}));
    let risk_vector = observed
        .and_then(|o| o.get("overallRisk").filter(|v| python_truthy(v)))
        .or_else(|| observed.and_then(|o| o.get("riskVector").filter(|v| python_truthy(v))))
        .cloned()
        .unwrap_or(Value::Null);

    let mut payload = json!({
        "status": status,
        "violations": evaluation.get("violations").cloned().unwrap_or_else(|| json!([])),
        "capacity": {
            "forecastFreeBytes": candidate.get("forecastFreeBytes").cloned().unwrap_or(Value::Null),
            "forecastStatus": forecast.and_then(|f| f.get("forecastStatus")).cloned().unwrap_or(Value::Null),
            "p50FreeBytes": forecast.and_then(|f| f.get("p50FreeBytes")).cloned().unwrap_or(Value::Null),
            "p90FreeBytes": forecast.and_then(|f| f.get("p90FreeBytes")).cloned().unwrap_or(Value::Null),
        },
        "cost": {
            "status": cost.get("status").cloned().unwrap_or(Value::Null),
            "monthlyCost": cost.get("monthlyCost").cloned().unwrap_or(Value::Null),
            "storage": cost.get("storage").cloned().unwrap_or(Value::Null),
            "egress": cost.get("egress").cloned().unwrap_or(Value::Null),
            "replicationTransfer": cost.get("replicationTransfer").cloned().unwrap_or(Value::Null),
            "retrieval": cost.get("retrieval").cloned().unwrap_or(Value::Null),
        },
        "failureDomains": candidate.get("failureDomains").cloned().unwrap_or(Value::Null),
        "blastRadius": {
            "committedCopies": candidate.get("committedCopies").cloned().unwrap_or(Value::Null),
            "minCommittedCopies": baseline.get("minCommittedCopies").cloned().unwrap_or(Value::Null),
            "passed": accepted,
        },
        "maintenanceWindows": inputs.get("maintenanceWindows").cloned().unwrap_or_else(|| json!([])),
        "riskVector": risk_vector,
        "runningEffects": inputs.get("runningEffects").cloned().unwrap_or_else(|| json!([])),
        "sourceSnapshotDigest": source_snapshot_digest,
        "forecastDigest": forecast.and_then(|f| f.get("forecastDigest")).cloned().unwrap_or(Value::Null),
        "priceCatalogDigest": price_catalog_digest,
        "authorityHeadDigest": authority_head_digest,
        "freshStateBundleDigest": inputs.get("freshStateBundleDigest").cloned().unwrap_or(Value::Null),
        "optimizerInputDigest": inputs.get("optimizerInputDigest").cloned().unwrap_or(Value::Null),
        "candidatePlan": candidate_plan,
        "simulation": simulation,
        "sideEffectsObserved": changed_domains.len(),
        "generatedAt": now_iso,
        "s3PutCount": 0,
        "s3DeleteCount": 0,
        "authorityMutationCount": 0,
        "actionJournalMutationCount": 0,
        "policyMutationCount": 0,
        "targetMutationCount": 0,
    });
    let whatif_digest = sha256_compact(&payload);
    payload["whatIfDigest"] = Value::String(whatif_digest);
    Ok(payload)
}
