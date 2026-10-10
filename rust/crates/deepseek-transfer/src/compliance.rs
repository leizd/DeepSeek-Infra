//! Replica lag, replication compliance, and maintenance-window observations.
//! These functions do not contact a provider or move payload bytes.

use crate::pycompat::{
    array_field, object_field, object_opt, parse_iso, python_int, python_int_or,
    python_str_or_empty, python_text, python_truthy,
};
use serde_json::{Map, Value, json};

const TERMINAL_PHASES: &[&str] = &["committed", "failed-terminal", "failed", "superseded"];

pub fn replay_replica_compliance_case(case: &Value, now: &str) -> Value {
    let op = case.get("op").and_then(Value::as_str).unwrap_or_default();
    let now = case.get("now").and_then(Value::as_str).unwrap_or(now);
    match op {
        "lag" => lag(case),
        "compliance" => compliance(case),
        "maintenance-window" => json!({ "inside": maintenance_window(case, now) }),
        _ => json!({ "status": "error", "error": format!("unknown-op:{op}") }),
    }
}

fn lag(case: &Value) -> Value {
    match lag_result(case) {
        Ok(value) => value,
        Err(name) => json!({ "status": "error", "oracle_exception": name }),
    }
}

fn lag_result(case: &Value) -> Result<Value, &'static str> {
    let copies = array_field(case, "copies");
    let primary_target_id = match case.get("primary_target_id") {
        Some(value) => python_text(Some(value)),
        None => "managed-local".to_string(),
    };
    let replica_target_id = python_text(case.get("replica_target_id"));
    let p_copies: Vec<Value> = copies
        .iter()
        .filter(|copy| {
            python_text(copy.get("targetId")) == primary_target_id
                && python_truthy(copy.get("recoverable").unwrap_or(&Value::Null))
        })
        .cloned()
        .collect();
    let r_copies: Vec<Value> = copies
        .iter()
        .filter(|copy| {
            python_text(copy.get("targetId")) == replica_target_id
                && python_truthy(copy.get("recoverable").unwrap_or(&Value::Null))
        })
        .cloned()
        .collect();
    let mut primary_pt = object_opt(case.get("primary_pt"));
    if primary_pt.is_none() {
        primary_pt = p_copies.first().and_then(Value::as_object).cloned();
    }
    let mut replica_pt = object_opt(case.get("replica_pt"));
    if replica_pt.is_none() {
        replica_pt = r_copies.first().and_then(Value::as_object).cloned();
    }
    let Some(primary_pt) = primary_pt else {
        return Ok(json!({ "lagRecoveryPoints": 0, "lagSeconds": 0, "status": "no-primary" }));
    };
    let Some(replica_pt) = replica_pt else {
        return Ok(
            json!({ "lagRecoveryPoints": 999, "lagSeconds": 999999, "status": "no-replica" }),
        );
    };
    let mut lag_seconds = 0_i64;
    if let (Some(p_time), Some(r_time)) = (
        parse_iso(primary_pt.get("committedAt")),
        parse_iso(replica_pt.get("committedAt")),
    ) {
        lag_seconds = (p_time - r_time).max(0);
    }
    let p_backups = unique_ids(backup_ids(&p_copies)?);
    let r_backups = unique_ids(backup_ids(&r_copies)?);
    let lag_points = p_backups
        .iter()
        .filter(|id| !r_backups.contains(id))
        .count();
    Ok(json!({
        "lagRecoveryPoints": lag_points,
        "lagSeconds": lag_seconds,
        "primaryCommittedAt": primary_pt.get("committedAt").cloned().unwrap_or(Value::Null),
        "replicaCommittedAt": replica_pt.get("committedAt").cloned().unwrap_or(Value::Null),
        "status": "calculated",
    }))
}

fn unique_ids(ids: Vec<String>) -> Vec<String> {
    let mut unique = Vec::new();
    for id in ids {
        if !unique.contains(&id) {
            unique.push(id);
        }
    }
    unique
}

fn backup_ids(copies: &[Value]) -> Result<Vec<String>, &'static str> {
    let mut ids = Vec::new();
    for copy in copies {
        let Some(map) = copy.as_object() else {
            return Err("TypeError");
        };
        let Some(backup_id) = map.get("backupId") else {
            return Err("KeyError");
        };
        ids.push(python_text(Some(backup_id)));
    }
    Ok(ids)
}

fn compliance(case: &Value) -> Value {
    let policy = object_field(case, "policy");
    let copies = array_field(case, "copies");
    let jobs = array_field(case, "jobs");
    let replication = match policy.get("replication") {
        Some(Value::Object(map)) => map.clone(),
        _ => Map::new(),
    };
    if replication.is_empty() || !python_truthy(replication.get("enabled").unwrap_or(&Value::Null))
    {
        return json!({
            "enabled": false,
            "compliance": "healthy",
            "committedCopies": 1,
            "requiredCopies": 1,
        });
    }
    let required = match python_int_or(replication.get("minCommittedCopies"), 1) {
        Ok(value) => value,
        Err(name) => return json!({ "status": "error", "oracle_exception": name }),
    };
    let primary_target = match policy.get("targetId") {
        Some(value) if python_truthy(value) => python_text(Some(value)),
        _ => "managed-local".to_string(),
    };
    let committed: Vec<Value> = copies
        .iter()
        .filter(|copy| {
            python_truthy(copy.get("recoverable").unwrap_or(&Value::Null))
                && copy.get("state") == Some(&json!("healthy"))
        })
        .cloned()
        .collect();
    let open_required = jobs
        .iter()
        .filter(|job| {
            python_text(job.get("mode")) == "required"
                && !TERMINAL_PHASES.contains(&python_str_or_empty(job.get("phase")).as_str())
        })
        .count();
    let failed_required = jobs
        .iter()
        .filter(|job| {
            python_text(job.get("mode")) == "required"
                && matches!(
                    python_str_or_empty(job.get("phase")).as_str(),
                    "failed" | "failed-terminal"
                )
        })
        .count();
    let mut compliance = "healthy";
    let mut reasons = Vec::new();
    if (committed.len() as i64) < required {
        compliance = "degraded";
        reasons.push(Value::String("insufficient-committed-copies".into()));
    }
    if open_required > 0 {
        compliance = "degraded";
        reasons.push(Value::String("open-required-jobs".into()));
    }
    if failed_required > 0 {
        compliance = "degraded";
        reasons.push(Value::String("failed-required-jobs".into()));
    }
    let objectives = match policy.get("recoveryObjectives") {
        Some(Value::Object(map)) => map.clone(),
        _ => Map::new(),
    };
    let max_lag = match objectives.get("maxReplicaLagSeconds") {
        Some(value) if python_truthy(value) => Some(value.clone()),
        _ => replication.get("maxReplicaLagSeconds").cloned(),
    };
    if let Some(max_lag) = max_lag {
        let max_lag = match python_int(&max_lag) {
            Ok(value) => value,
            Err(name) => return json!({ "status": "error", "oracle_exception": name }),
        };
        let targets = match replication.get("targets") {
            Some(Value::Array(items)) => items.clone(),
            _ => Vec::new(),
        };
        for entry in targets {
            let Some(map) = entry.as_object() else {
                continue;
            };
            if !python_truthy(map.get("targetId").unwrap_or(&Value::Null)) {
                continue;
            }
            let t_id = python_text(map.get("targetId"));
            let mut lag_case = Map::new();
            lag_case.insert("copies".into(), Value::Array(copies.clone()));
            lag_case.insert("replica_target_id".into(), Value::String(t_id.clone()));
            lag_case.insert(
                "primary_target_id".into(),
                Value::String(primary_target.clone()),
            );
            if let Some(value) = case.get("primary_pt") {
                lag_case.insert("primary_pt".into(), value.clone());
            }
            if let Some(value) = case.get("replica_pt") {
                lag_case.insert("replica_pt".into(), value.clone());
            }
            let lag_info = lag(&Value::Object(lag_case));
            let lag_seconds = lag_info
                .get("lagSeconds")
                .and_then(Value::as_i64)
                .unwrap_or(0);
            if lag_seconds > max_lag {
                compliance = "degraded";
                reasons.push(Value::String(format!("replica-lag-exceeded:{t_id}")));
            }
        }
    }
    json!({
        "enabled": true,
        "compliance": compliance,
        "reasons": reasons,
        "committedCopies": committed.len(),
        "requiredCopies": required,
        "healthyCopies": committed.len(),
        "openRequiredJobs": open_required,
        "failedRequiredJobs": failed_required,
        "available": !committed.is_empty(),
    })
}

fn maintenance_window(case: &Value, now: &str) -> bool {
    policy_inside_maintenance_window(&object_field(case, "policy"), now)
}

pub(crate) fn policy_inside_maintenance_window(policy: &Map<String, Value>, now: &str) -> bool {
    let placement = match policy.get("placement") {
        Some(Value::Object(map)) => map.clone(),
        _ => Map::new(),
    };
    let Some(Value::Object(window)) = placement.get("maintenanceWindow") else {
        return true;
    };
    if window.is_empty() {
        return true;
    }
    let start_str = python_str_or_empty(window.get("start"));
    let start_str = if start_str.is_empty() {
        "00:00".to_string()
    } else {
        start_str
    };
    let end_str = python_str_or_empty(window.get("end"));
    let end_str = if end_str.is_empty() {
        "23:59".to_string()
    } else {
        end_str
    };
    let Some(current) = parse_iso(Some(&Value::String(now.to_string()))) else {
        return true;
    };
    let seconds = current.rem_euclid(86400);
    let cur_mins = (seconds / 3600) * 60 + (seconds % 3600) / 60;
    match parse_clock(&start_str).zip(parse_clock(&end_str)) {
        Some((start_mins, end_mins)) => {
            if start_mins <= end_mins {
                start_mins <= cur_mins && cur_mins <= end_mins
            } else {
                cur_mins >= start_mins || cur_mins <= end_mins
            }
        }
        None => true,
    }
}

fn parse_clock(value: &str) -> Option<i64> {
    let parts: Vec<&str> = value.split(':').collect();
    if parts.len() != 2 {
        return None;
    }
    let hour: i64 = parts[0].parse().ok()?;
    let minute: i64 = parts[1].parse().ok()?;
    Some(hour * 60 + minute)
}
