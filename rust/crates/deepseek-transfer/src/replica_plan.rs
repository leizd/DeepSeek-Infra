//! Replica reconcile/rebalance planners: cursor, scan, needs-repair, and candidate selection.
//! These functions do not execute repairs, create jobs, contact a provider, or move payload bytes.

use crate::compliance::policy_inside_maintenance_window;
use crate::pycompat::{
    object_field, python_float, python_float_or, python_int_or, python_text, python_truthy,
};
use serde_json::{Map, Value, json};
use std::collections::HashMap;

const DEFAULT_NOW: &str = "2026-09-05T15:00:00Z";

pub fn replay_replica_planner_case(case: &Value) -> Value {
    let op = case.get("op").and_then(Value::as_str).unwrap_or_default();
    match op {
        "reconcile-plan" => match reconcile_plan(case) {
            Ok(value) => value,
            Err(name) => json!({ "status": "error", "oracle_exception": name }),
        },
        "rebalance-plan" => match rebalance_plan(case) {
            Ok(value) => value,
            Err(name) => json!({ "status": "error", "oracle_exception": name }),
        },
        _ => json!({ "status": "error", "error": format!("unknown-op:{op}") }),
    }
}

fn reconcile_plan(case: &Value) -> Result<Value, &'static str> {
    let policy_id = match case.get("policy_id") {
        Some(value) => python_text(Some(value)),
        None => "pol-1".to_string(),
    };
    let policy = object_field(case, "policy");
    let copies = python_list(case.get("copies"))?;
    let retired = retired_ids(case)?;
    let max_points = case.get("max_points").cloned().unwrap_or(json!(20));
    let max_repairs = case.get("max_repairs").cloned().unwrap_or(json!(2));

    let replication = match policy.get("replication") {
        Some(Value::Object(map)) => map.clone(),
        _ => Map::new(),
    };
    if replication.is_empty() || !python_truthy(replication.get("enabled").unwrap_or(&Value::Null))
    {
        return Ok(json!({
            "status": "skipped",
            "reason": "replication-disabled",
            "policyId": policy_id,
        }));
    }

    let target_entries = python_list(replication.get("targets"))?;
    if target_entries.is_empty() {
        return Ok(json!({ "status": "noop", "policyId": policy_id }));
    }

    let mut expected_targets = Vec::new();
    for entry in &target_entries {
        let Some(map) = entry.as_object() else {
            continue;
        };
        if !python_truthy(map.get("targetId").unwrap_or(&Value::Null)) {
            continue;
        }
        expected_targets.push(python_text(map.get("targetId")));
    }
    let configured_primary = first_truthy_text(
        &[policy.get("primaryTargetId"), policy.get("targetId")],
        "managed-local",
    );
    if !expected_targets
        .iter()
        .any(|tid| tid == &configured_primary)
    {
        expected_targets.push(configured_primary);
    }
    if expected_targets.is_empty() {
        return Ok(json!({ "status": "noop", "policyId": policy_id }));
    }

    let by_backup = group_by_backup(&copies)?;
    let mut sorted_backup_ids: Vec<String> = by_backup.keys().cloned().collect();
    sorted_backup_ids.sort_by_key(|backup_id| point_sort_key(backup_id, &by_backup[backup_id]));

    let after_committed_at = case.get("after_committed_at");
    let after_logical_id = case.get("after_logical_id");
    let mut filtered_backup_ids = Vec::new();
    if after_committed_at.is_some_and(python_truthy) || after_logical_id.is_some_and(python_truthy)
    {
        let target_tuple = (
            str_or_empty(after_committed_at),
            str_or_empty(after_logical_id),
        );
        for backup_id in &sorted_backup_ids {
            if point_sort_key(backup_id, &by_backup[backup_id]) > target_tuple {
                filtered_backup_ids.push(backup_id.clone());
            }
        }
    } else {
        filtered_backup_ids = sorted_backup_ids.clone();
    }

    let mut wrapped = false;
    if filtered_backup_ids.is_empty() && !sorted_backup_ids.is_empty() {
        filtered_backup_ids = sorted_backup_ids.clone();
        wrapped = true;
    }

    let mut scanned: i64 = 0;
    let mut repairs_triggered: i64 = 0;
    let mut last_scanned_backup_id: Option<String> = None;
    let mut last_scanned_committed_at: Option<String> = None;
    let mut planned = Vec::new();

    for backup_id in filtered_backup_ids {
        if python_ge_int(scanned, &max_points)? {
            break;
        }
        let copy_list = by_backup.get(&backup_id).cloned().unwrap_or_default();
        if retired.contains(&backup_id) {
            continue;
        }
        scanned += 1;
        last_scanned_backup_id = Some(backup_id.clone());
        last_scanned_committed_at = Some(str_or_empty(
            copy_list.first().and_then(|copy| copy.get("committedAt")),
        ));

        let mut existing_targets: HashMap<String, Value> = HashMap::new();
        for copy in &copy_list {
            let Some(map) = copy.as_object() else {
                return Err("TypeError");
            };
            let Some(target_id) = map.get("targetId") else {
                return Err("KeyError");
            };
            existing_targets.insert(python_text(Some(target_id)), copy.clone());
        }
        let healthy_sources: Vec<Value> = copy_list
            .iter()
            .filter(|copy| {
                python_truthy(copy.get("recoverable").unwrap_or(&Value::Null))
                    && copy.get("state") == Some(&json!("healthy"))
            })
            .cloned()
            .collect();
        if healthy_sources.is_empty() {
            continue;
        }
        let Some(source_map) = healthy_sources[0].as_object() else {
            return Err("TypeError");
        };
        let Some(source_target) = source_map.get("targetId") else {
            return Err("KeyError");
        };
        let source_target_id = python_text(Some(source_target));

        for dest_tid in &expected_targets {
            if python_ge_int(repairs_triggered, &max_repairs)? {
                break;
            }
            let needs_repair = match existing_targets.get(dest_tid) {
                None => true,
                Some(existing) => {
                    !python_truthy(existing.get("recoverable").unwrap_or(&Value::Null))
                        || existing.get("state") != Some(&json!("healthy"))
                }
            };
            if needs_repair {
                repairs_triggered += 1;
                planned.push(json!({
                    "backupId": backup_id,
                    "destTargetId": dest_tid,
                    "sourceTargetId": source_target_id,
                }));
            }
        }
    }

    let cursor_written = last_scanned_backup_id.is_some();
    Ok(json!({
        "status": "completed",
        "policyId": policy_id,
        "scannedPoints": scanned,
        "repairsTriggered": repairs_triggered,
        "wrappedAround": wrapped,
        "cursorWritten": cursor_written,
        "afterCommittedAt": last_scanned_committed_at,
        "afterLogicalId": last_scanned_backup_id,
        "plannedRepairs": planned,
    }))
}

fn rebalance_plan(case: &Value) -> Result<Value, &'static str> {
    let policy = object_field(case, "policy");
    let copies = python_list(case.get("copies"))?;
    let target_records = python_list(case.get("targets"))?;
    let max_jobs = case.get("max_jobs").cloned().unwrap_or(json!(5));
    let now = case
        .get("now")
        .and_then(Value::as_str)
        .unwrap_or(DEFAULT_NOW);

    let replication = match policy.get("replication") {
        Some(Value::Object(map)) => map.clone(),
        _ => Map::new(),
    };
    if replication.is_empty() || !python_truthy(replication.get("enabled").unwrap_or(&Value::Null))
    {
        return Ok(json!({ "status": "skipped", "reason": "replication-disabled" }));
    }
    if !policy_inside_maintenance_window(&policy, now) {
        return Ok(json!({ "status": "skipped", "reason": "outside-maintenance-window" }));
    }

    let min_fd = python_int_or(replication.get("minFailureDomains"), 1)?;
    let placement = mapping_or_empty(policy.get("placement"))?;
    let max_copies_per_fd = match placement.get("maxCopiesPerFailureDomain") {
        Some(value) if python_truthy(value) => Some(value.clone()),
        _ => match replication.get("maxCopiesPerFailureDomain") {
            None | Some(Value::Null) => None,
            Some(value) => Some(value.clone()),
        },
    };
    let soft_watermark = python_float_or(placement.get("softWatermarkPercent"), 80.0)?;

    let target_entries = python_list(replication.get("targets"))?;
    let all_target_records = index_target_records(&target_records)?;
    let mut active_targets = Vec::new();
    for entry in &target_entries {
        let Some(map) = entry.as_object() else {
            continue;
        };
        if !python_truthy(map.get("targetId").unwrap_or(&Value::Null)) {
            continue;
        }
        let cand = python_text(map.get("targetId"));
        let record = lookup_target(&all_target_records, &cand);
        if record.get("drainState") != Some(&json!("draining")) {
            active_targets.push(cand);
        }
    }

    let by_backup = group_by_backup_ordered(&copies)?;
    let mut jobs_created: i64 = 0;
    let mut planned = Vec::new();

    for (backup_id, copy_list) in by_backup {
        if python_ge_int(jobs_created, &max_jobs)? {
            break;
        }
        let healthy: Vec<Value> = copy_list
            .iter()
            .filter(|copy| {
                python_truthy(copy.get("recoverable").unwrap_or(&Value::Null))
                    && copy.get("state") == Some(&json!("healthy"))
            })
            .cloned()
            .collect();
        if healthy.is_empty() {
            continue;
        }
        let mut healthy_target_ids = Vec::new();
        for copy in &healthy {
            let Some(map) = copy.as_object() else {
                return Err("TypeError");
            };
            let Some(target_id) = map.get("targetId") else {
                return Err("KeyError");
            };
            let tid = python_text(Some(target_id));
            if !healthy_target_ids.contains(&tid) {
                healthy_target_ids.push(tid);
            }
        }
        let mut current_fds = Vec::new();
        for tid in &healthy_target_ids {
            let fd = field_or_default(
                &lookup_target(&all_target_records, tid),
                "failureDomain",
                "default",
            );
            if !current_fds.contains(&fd) {
                current_fds.push(fd);
            }
        }
        let has_draining = healthy_target_ids.iter().any(|tid| {
            lookup_target(&all_target_records, tid).get("drainState") == Some(&json!("draining"))
        });

        let mut has_capacity_pressure = false;
        let mut constrained_source_tid = None;
        for tid in &healthy_target_ids {
            let cap = lookup_target(&all_target_records, tid);
            if over_watermark(cap.get("freePercent"), soft_watermark)? {
                has_capacity_pressure = true;
                constrained_source_tid = Some(tid.clone());
                break;
            }
        }

        let needs_rebalance =
            (current_fds.len() as i64) < min_fd || has_draining || has_capacity_pressure;
        if !needs_rebalance {
            continue;
        }
        for cand_tid in &active_targets {
            if healthy_target_ids.contains(cand_tid) {
                continue;
            }
            let cand_fd = field_or_default(
                &lookup_target(&all_target_records, cand_tid),
                "failureDomain",
                "default",
            );
            let existing_in_fd = healthy_target_ids
                .iter()
                .filter(|tid| {
                    field_or_default(
                        &lookup_target(&all_target_records, tid),
                        "failureDomain",
                        "default",
                    ) == cand_fd
                })
                .count() as i64;
            if let Some(raw) = &max_copies_per_fd {
                let limit = crate::pycompat::python_int(raw)?;
                if limit > 0 && existing_in_fd + 1 > limit {
                    continue;
                }
            }
            let cand_cap = lookup_target(&all_target_records, cand_tid);
            if over_watermark(cand_cap.get("freePercent"), soft_watermark)? {
                continue;
            }
            if current_fds.contains(&cand_fd) && !has_draining && !has_capacity_pressure {
                continue;
            }
            let src_tid = match &constrained_source_tid {
                Some(tid) => tid.clone(),
                None => {
                    let Some(source_map) = healthy[0].as_object() else {
                        return Err("TypeError");
                    };
                    let Some(source_target) = source_map.get("targetId") else {
                        return Err("KeyError");
                    };
                    python_text(Some(source_target))
                }
            };
            let reason = if has_draining {
                "drain-migration"
            } else if has_capacity_pressure {
                "proactive-capacity-rebalance"
            } else {
                "failure-domain-diversity"
            };
            planned.push(json!({
                "backupId": backup_id,
                "destTargetId": cand_tid,
                "sourceTargetId": src_tid,
                "reason": reason,
                "pruneSourceAfter": has_draining || has_capacity_pressure,
            }));
            jobs_created += 1;
            break;
        }
    }

    Ok(json!({
        "status": "completed",
        "jobsCreated": jobs_created,
        "plannedJobs": planned,
    }))
}

fn python_list(value: Option<&Value>) -> Result<Vec<Value>, &'static str> {
    match value {
        None => Ok(Vec::new()),
        Some(value) if !python_truthy(value) => Ok(Vec::new()),
        Some(Value::Array(items)) => Ok(items.clone()),
        _ => Err("TypeError"),
    }
}

fn retired_ids(case: &Value) -> Result<Vec<String>, &'static str> {
    let items = python_list(case.get("retired"))?;
    Ok(items.iter().map(|item| python_text(Some(item))).collect())
}

fn group_by_backup(copies: &[Value]) -> Result<HashMap<String, Vec<Value>>, &'static str> {
    let mut by_backup: HashMap<String, Vec<Value>> = HashMap::new();
    for copy in copies {
        let Some(map) = copy.as_object() else {
            return Err("TypeError");
        };
        let Some(backup_id) = map.get("backupId") else {
            return Err("KeyError");
        };
        by_backup
            .entry(python_text(Some(backup_id)))
            .or_default()
            .push(copy.clone());
    }
    Ok(by_backup)
}

fn group_by_backup_ordered(copies: &[Value]) -> Result<Vec<(String, Vec<Value>)>, &'static str> {
    let mut order = Vec::new();
    let mut by_backup: HashMap<String, Vec<Value>> = HashMap::new();
    for copy in copies {
        let Some(map) = copy.as_object() else {
            return Err("TypeError");
        };
        let Some(backup_id) = map.get("backupId") else {
            return Err("KeyError");
        };
        let backup_id = python_text(Some(backup_id));
        if !by_backup.contains_key(&backup_id) {
            order.push(backup_id.clone());
        }
        by_backup.entry(backup_id).or_default().push(copy.clone());
    }
    Ok(order
        .into_iter()
        .map(|backup_id| {
            let copies = by_backup.remove(&backup_id).unwrap_or_default();
            (backup_id, copies)
        })
        .collect())
}

fn point_sort_key(backup_id: &str, copies: &[Value]) -> (String, String) {
    let cat = copies
        .iter()
        .filter(|copy| python_truthy(copy.get("committedAt").unwrap_or(&Value::Null)))
        .map(|copy| python_text(copy.get("committedAt")))
        .min()
        .unwrap_or_default();
    (cat, backup_id.to_string())
}

fn str_or_empty(value: Option<&Value>) -> String {
    match value {
        Some(value) if python_truthy(value) => python_text(Some(value)),
        _ => String::new(),
    }
}

fn first_truthy_text(values: &[Option<&Value>], fallback: &str) -> String {
    for value in values.iter().flatten() {
        if python_truthy(value) {
            return python_text(Some(value));
        }
    }
    fallback.to_string()
}

fn python_ge_int(left: i64, right: &Value) -> Result<bool, &'static str> {
    match right {
        Value::Bool(true) => Ok(left >= 1),
        Value::Bool(false) => Ok(left >= 0),
        Value::Number(number) => {
            if let Some(value) = number.as_i64() {
                Ok(left >= value)
            } else if let Some(value) = number.as_f64() {
                Ok((left as f64) >= value)
            } else {
                Err("TypeError")
            }
        }
        _ => Err("TypeError"),
    }
}

fn mapping_or_empty(value: Option<&Value>) -> Result<Map<String, Value>, &'static str> {
    match value {
        None => Ok(Map::new()),
        Some(value) if !python_truthy(value) => Ok(Map::new()),
        Some(Value::Object(map)) => Ok(map.clone()),
        Some(_) => Err("AttributeError"),
    }
}

fn index_target_records(
    records: &[Value],
) -> Result<HashMap<Value, Map<String, Value>>, &'static str> {
    let mut indexed = HashMap::new();
    for record in records {
        let Some(map) = record.as_object() else {
            return Err("TypeError");
        };
        let Some(target_id) = map.get("targetId") else {
            return Err("KeyError");
        };
        if target_id.is_array() || target_id.is_object() {
            return Err("TypeError");
        }
        indexed.insert(target_id.clone(), map.clone());
    }
    Ok(indexed)
}

fn lookup_target(records: &HashMap<Value, Map<String, Value>>, tid: &str) -> Map<String, Value> {
    records
        .get(&Value::String(tid.to_string()))
        .cloned()
        .unwrap_or_default()
}

fn field_or_default(record: &Map<String, Value>, key: &str, default: &str) -> String {
    match record.get(key) {
        Some(value) if python_truthy(value) => python_text(Some(value)),
        _ => default.to_string(),
    }
}

fn over_watermark(free_percent: Option<&Value>, soft_watermark: f64) -> Result<bool, &'static str> {
    let Some(free_percent) = free_percent else {
        return Ok(false);
    };
    if free_percent.is_null() {
        return Ok(false);
    }
    let pct = python_float(free_percent)?;
    Ok(100.0 - pct >= soft_watermark)
}
