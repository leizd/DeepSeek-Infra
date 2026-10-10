//! Rebalance execute/drain prefix and prune-safety hold semantics.
//! These functions do not contact a provider or move payload bytes.

use crate::pycompat::{
    array_field, extra_from, format_iso, object_field, object_opt, parse_iso, phase_text,
    python_int, python_int_or, python_str_or_empty, python_text, python_truthy, set_phase,
};
use serde_json::{Map, Value, json};

pub fn replay_rebalance_hold_case(case: &Value, now: &str) -> Value {
    let op = case.get("op").and_then(Value::as_str).unwrap_or_default();
    match op {
        "hold-document" => json!({ "hold": hold_document(case, now) }),
        "hold-renew" => hold_renew(case, now),
        "is-source-held" => json!({ "held": is_source_held(case, now) }),
        "has-source-holds" => json!({ "held": has_source_holds(case, now) }),
        "simulate-removal" => simulate_removal(case, now),
        "claim-rebalance" => claim_rebalance(case, now),
        "fail-rebalance" => fail_rebalance(case, now),
        "classify-pending-rebalance" => classify_rebalance(case),
        "drain-limit" => drain_limit(case),
        _ => json!({ "status": "error", "error": format!("unknown-op:{op}") }),
    }
}

fn hold_document(case: &Value, now: &str) -> Value {
    let fields = object_field(case, "fields");
    let hold_seconds = match fields.get("holdSeconds") {
        None => 3600,
        Some(value) => python_int(value).unwrap_or(3600),
    };
    let current = parse_iso(Some(&Value::String(now.to_string()))).expect("frozen now");
    json!({
        "holderKind": "replica-repair",
        "holderId": fields.get("holderId").cloned().unwrap_or(Value::Null),
        "holdId": fields.get("holdId").cloned().unwrap_or(Value::Null),
        "targetId": fields.get("targetId").cloned().unwrap_or(Value::Null),
        "policyId": fields.get("policyId").cloned().unwrap_or(Value::Null),
        "backupId": fields.get("backupId").cloned().unwrap_or(Value::Null),
        "objectSetDigest": fields.get("objectSetDigest").cloned().unwrap_or(Value::Null),
        "createdAt": now,
        "expiresAt": format_iso(current + hold_seconds),
        "generation": 1,
        "etag": null,
    })
}

fn hold_renew(case: &Value, now: &str) -> Value {
    let mut hold = object_field(case, "hold");
    let duration = match case.get("durationSeconds") {
        None => 3600,
        Some(value) => match python_int(value) {
            Ok(value) => value,
            Err(name) => return json!({ "status": "error", "oracle_exception": name }),
        },
    };
    let Some(generation) = hold.get("generation").cloned() else {
        return json!({ "status": "error", "oracle_exception": "KeyError" });
    };
    let next_generation = match python_int(&generation) {
        Ok(value) => match value.checked_add(1) {
            Some(value) => value,
            None => return json!({ "status": "error", "oracle_exception": "OverflowError" }),
        },
        Err(name) => return json!({ "status": "error", "oracle_exception": name }),
    };
    let current = parse_iso(Some(&Value::String(now.to_string()))).expect("frozen now");
    hold.insert(
        "expiresAt".to_string(),
        Value::String(format_iso(current + duration)),
    );
    hold.insert("generation".to_string(), json!(next_generation));
    json!({ "hold": hold })
}

fn is_source_held(case: &Value, now: &str) -> bool {
    let current = parse_iso(Some(&Value::String(now.to_string()))).expect("frozen now");
    let target_id = python_text(case.get("target_id"));
    let policy_id = python_text(case.get("policy_id"));
    let backup_id = python_text(case.get("backup_id"));
    for item in array_field(case, "holds") {
        if item.get("decode_error").is_some_and(python_truthy) || !item.is_object() {
            continue;
        }
        if parse_iso(item.get("expiresAt")).is_some_and(|exp| current > exp) {
            continue;
        }
        if python_text(item.get("targetId")) == target_id
            && python_text(item.get("policyId")) == policy_id
            && python_text(item.get("backupId")) == backup_id
        {
            return true;
        }
    }
    false
}

fn has_source_holds(case: &Value, now: &str) -> bool {
    let current = parse_iso(Some(&Value::String(now.to_string()))).expect("frozen now");
    let target_id = python_text(case.get("target_id"));
    for item in array_field(case, "holds") {
        if item.get("decode_error").is_some_and(python_truthy) || !item.is_object() {
            return true;
        }
        let expiry = parse_iso(item.get("expiresAt"));
        let active = expiry.is_none() || expiry.is_some_and(|exp| current <= exp);
        if python_str_or_empty(item.get("targetId")) == target_id && active {
            return true;
        }
    }
    false
}

fn simulate_removal(case: &Value, now: &str) -> Value {
    let policy = object_field(case, "policy");
    let target_id = python_text(case.get("target_id"));
    let replication = match policy.get("replication") {
        Some(Value::Object(map)) => map.clone(),
        _ => Map::new(),
    };
    let placement = match policy.get("placement") {
        Some(Value::Object(map)) => map.clone(),
        _ => Map::new(),
    };
    let min_copies = match python_int_or(replication.get("minCommittedCopies"), 1) {
        Ok(value) => value,
        Err(name) => return json!({ "status": "error", "oracle_exception": name }),
    };
    let min_fd = match python_int_or(replication.get("minFailureDomains"), 1) {
        Ok(value) => value,
        Err(name) => return json!({ "status": "error", "oracle_exception": name }),
    };
    let min_regions = match python_int_or(replication.get("minRegions"), 1) {
        Ok(value) => value,
        Err(name) => return json!({ "status": "error", "oracle_exception": name }),
    };
    let max_copies = match placement.get("maxCopiesPerFailureDomain") {
        Some(value) if python_truthy(value) => Some(value.clone()),
        _ => replication.get("maxCopiesPerFailureDomain").cloned(),
    };
    let mut records = Map::new();
    for item in array_field(case, "targets") {
        let Some(map) = item.as_object() else {
            continue;
        };
        let Some(id) = map.get("targetId") else {
            return json!({ "status": "error", "oracle_exception": "KeyError" });
        };
        records.insert(python_text(Some(id)), Value::Object(map.clone()));
    }
    let copies = array_field(case, "copies");
    let healthy_before: Vec<Value> = copies
        .iter()
        .filter(|copy| {
            python_truthy(copy.get("recoverable").unwrap_or(&Value::Null))
                && copy.get("state") == Some(&json!("healthy"))
        })
        .cloned()
        .collect();
    let healthy_after: Vec<Value> = healthy_before
        .iter()
        .filter(|copy| python_text(copy.get("targetId")) != target_id)
        .cloned()
        .collect();
    let fd_before = unique_labels(&healthy_before, &records, "failureDomain", "default");
    let fd_after = unique_labels(&healthy_after, &records, "failureDomain", "default");
    let regions_before = unique_labels(&healthy_before, &records, "region", "default-region");
    let regions_after = unique_labels(&healthy_after, &records, "region", "default-region");
    let mut counts_by_fd_after = Map::new();
    for copy in &healthy_after {
        let fd_name = label_of(copy, &records, "failureDomain", "default");
        let next = counts_by_fd_after
            .get(&fd_name)
            .and_then(Value::as_i64)
            .unwrap_or(0)
            + 1;
        counts_by_fd_after.insert(fd_name, json!(next));
    }
    let mut policy_safe = healthy_after.len() as i64 >= min_copies
        && fd_after.len() as i64 >= min_fd
        && regions_after.len() as i64 >= min_regions;
    if let Some(max_copies) = max_copies {
        match python_int(&max_copies) {
            Ok(max) if max > 0 => {
                if counts_by_fd_after
                    .values()
                    .any(|cnt| cnt.as_i64().unwrap_or(0) > max)
                {
                    policy_safe = false;
                }
            }
            Ok(_) => {}
            Err(name) => return json!({ "status": "error", "oracle_exception": name }),
        }
    }
    let held = is_source_held(case, now);
    json!({
        "healthyCopiesBefore": healthy_before.len(),
        "healthyCopiesAfter": healthy_after.len(),
        "failureDomainsBefore": fd_before.len(),
        "failureDomainsAfter": fd_after.len(),
        "regionsBefore": regions_before.len(),
        "regionsAfter": regions_after.len(),
        "copiesInEachDomainAfter": counts_by_fd_after,
        "policySafe": policy_safe && !held,
        "protectedByHold": held,
        "targetId": case.get("target_id").cloned().unwrap_or(Value::Null),
        "backupId": case.get("backup_id").cloned().unwrap_or(Value::Null),
    })
}

fn unique_labels(
    copies: &[Value],
    records: &Map<String, Value>,
    field: &str,
    default: &str,
) -> Vec<String> {
    let mut labels = Vec::new();
    for copy in copies {
        let label = label_of(copy, records, field, default);
        if !labels.contains(&label) {
            labels.push(label);
        }
    }
    labels
}

fn label_of(copy: &Value, records: &Map<String, Value>, field: &str, default: &str) -> String {
    let key = python_text(copy.get("targetId"));
    let record = records.get(&key).and_then(Value::as_object);
    or_default(record.and_then(|map| map.get(field)), default)
}

fn or_default(value: Option<&Value>, default: &str) -> String {
    match value {
        Some(value) if python_truthy(value) => python_text(Some(value)),
        _ => default.to_string(),
    }
}

fn claim_rebalance(case: &Value, now: &str) -> Value {
    let job_id = case.get("job_id").cloned().unwrap_or(Value::Null);
    let Some(job) = object_opt(case.get("job")) else {
        return json!({ "status": "error", "error": "not-found", "jobId": job_id });
    };
    let phase = phase_text(&job);
    if phase == "cancelled" {
        return json!({ "status": "cancelled", "jobId": job_id, "job": job });
    }
    if phase == "complete" {
        return json!({ "status": "success", "jobId": job_id, "job": job });
    }
    let job = set_phase(job, json!("transferring"), Map::new(), now);
    json!({ "status": "claimed", "jobId": job_id, "job": job })
}

fn fail_rebalance(case: &Value, now: &str) -> Value {
    let job = object_field(case, "job");
    let job_id = case
        .get("job_id")
        .cloned()
        .unwrap_or_else(|| job.get("jobId").cloned().unwrap_or(Value::Null));
    let message = python_str_or_empty(case.get("error"));
    let job = set_phase(
        job,
        json!("failed"),
        extra_from([("error", Value::String(message.clone()))]),
        now,
    );
    json!({ "status": "failed", "jobId": job_id, "error": message, "job": job })
}

fn classify_rebalance(case: &Value) -> Value {
    let job = object_field(case, "job");
    if phase_text(&job) == "pending" {
        json!({ "decision": "pending", "job": job })
    } else {
        json!({ "decision": "skip", "job": job })
    }
}

fn drain_limit(case: &Value) -> Value {
    let limit = match case.get("limit") {
        None => return json!({ "status": "error", "oracle_exception": "TypeError" }),
        Some(value) => match python_int(value) {
            Ok(value) => value,
            Err(name) => return json!({ "status": "error", "oracle_exception": name }),
        },
    };
    let take = 1.max(limit) as usize;
    let pending: Vec<Value> = array_field(case, "jobs")
        .into_iter()
        .filter(|job| python_str_or_empty(job.get("phase")) == "pending")
        .collect();
    let pending_count = pending.len();
    let selected: Vec<Value> = pending
        .into_iter()
        .take(take)
        .map(|job| job.get("jobId").cloned().unwrap_or(Value::Null))
        .collect();
    json!({ "selected": selected, "pendingCount": pending_count })
}
