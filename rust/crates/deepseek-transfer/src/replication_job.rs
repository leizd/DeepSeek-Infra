//! BackupReplicationJob durable phase-machine semantics.
//! These functions do not contact a provider or move payload bytes.

use crate::pycompat::{
    array_field, extra_from, extra_map, format_iso, object_field, object_opt, parse_iso,
    phase_text, python_int_or, python_str_or_empty, python_text, python_truthy, set_phase,
};
use serde_json::{Map, Value, json};

const JOB_SCHEMA_VERSION: i64 = 2;
const DEFAULT_MAX_ATTEMPTS: i64 = 5;
const TERMINAL_PHASES: &[&str] = &["committed", "failed-terminal", "failed", "superseded"];
const ACTIVE_PHASES: &[&str] = &[
    "queued",
    "checking-target",
    "transferring-components",
    "components-verified",
    "writing-receipt",
    "committing",
    "retry-wait",
    "repair-needed",
];
const MODES: &[&str] = &["required", "best-effort"];
const RECEIPT_SNAPSHOT_KEYS: &[&str] = &[
    "snapshotKind",
    "parentBackupId",
    "baseBackupId",
    "lineageId",
    "chainDepth",
    "chunkProtocol",
    "logicalBytes",
    "size",
    "storageProtocol",
    "creationVerified",
];

pub fn replay_replication_phase_case(case: &Value, now: &str) -> Value {
    let op = case.get("op").and_then(Value::as_str).unwrap_or_default();
    match op {
        "backoff" => {
            json!({ "seconds": json_number(compute_replication_backoff_seconds(case_attempts(case))) })
        }
        "set-phase" => json!({
            "job": set_phase(object_field(case, "job"), case.get("phase").cloned().unwrap_or(Value::Null), extra_map(case), now)
        }),
        "enqueue" => enqueue(case, now),
        "claim" => claim(case, now),
        "fail" => fail_job(case, now),
        "classify-pending" => classify_pending(case, now),
        "has-open-required" => has_open_required(case),
        _ => json!({ "status": "error", "error": format!("unknown-op:{op}") }),
    }
}

pub fn compute_replication_backoff_seconds(attempts: i64) -> f64 {
    let exp = attempts.min(8);
    let Ok(exp) = i32::try_from(exp) else {
        return 300.0;
    };
    2_f64.powi(exp).min(300.0)
}

fn case_attempts(case: &Value) -> i64 {
    python_int_or(case.get("attempts"), 0).unwrap_or(0)
}

fn enqueue(case: &Value, now: &str) -> Value {
    let policy = object_field(case, "policy");
    let primary_target_id = python_str_or_empty(case.get("primary_target_id"));
    let replication_value = policy.get("replication");
    let replication = match replication_value {
        Some(Value::Object(map)) => map.clone(),
        _ => Map::new(),
    };
    if replication.is_empty() || !python_truthy(replication.get("enabled").unwrap_or(&Value::Null))
    {
        return json!({ "jobs": [] });
    }
    let mut targets = match replication.get("targets") {
        Some(Value::Array(items)) => items.clone(),
        Some(value) if python_truthy(value) => {
            // list(non-list) is not used in the corpus; treat as empty.
            Vec::new()
        }
        _ => Vec::new(),
    };
    let configured_primary_id = first_str(&[policy.get("primaryTargetId"), policy.get("targetId")])
        .trim()
        .to_string();
    let configured_ids: Vec<String> = targets
        .iter()
        .filter_map(Value::as_object)
        .map(|entry| python_str_or_empty(entry.get("targetId")))
        .collect();
    if !configured_primary_id.is_empty()
        && configured_primary_id != primary_target_id
        && !configured_ids.contains(&configured_primary_id)
    {
        targets.push(json!({
            "targetId": configured_primary_id,
            "mode": "required",
            "role": "failback-catchup",
        }));
    }
    if targets.is_empty() {
        return json!({ "jobs": [] });
    }

    let policy_id = python_str_or_empty(policy.get("policyId"));
    let backup_id = python_str_or_empty(case.get("backup_id"));
    let run_id = python_str_or_empty(case.get("run_id"));
    let schedule_slot = python_str_or_empty(case.get("schedule_slot"));
    let slot_digest = python_str_or_empty(case.get("slot_digest"));
    let primary_receipt = case.get("primary_receipt");
    let (object_set_digest, control_digest, objects) = receipt_inventory(primary_receipt);
    let snapshot = receipt_snapshot(primary_receipt);
    let existing_jobs = array_field(case, "existing_jobs");
    let mut job_ids = array_field(case, "job_ids").into_iter();
    let mut created = Vec::new();

    for entry in targets {
        let Some(entry) = entry.as_object() else {
            continue;
        };
        let replica_id = python_str_or_empty(entry.get("targetId"))
            .trim()
            .to_string();
        if replica_id.is_empty() || replica_id == primary_target_id {
            continue;
        }
        let raw_mode = first_str(&[entry.get("mode")]);
        let raw_mode = if raw_mode.is_empty() {
            "required".to_string()
        } else {
            raw_mode
        };
        let mode = if MODES.contains(&raw_mode.as_str()) {
            raw_mode
        } else {
            "required".to_string()
        };
        if let Some(existing) = existing_jobs.iter().find(|job| {
            python_str_or_empty(job.get("replicaTargetId")) == replica_id
                && !TERMINAL_PHASES.contains(&python_str_or_empty(job.get("phase")).as_str())
        }) {
            created.push(existing.clone());
            continue;
        }
        let Some(job_id) = job_ids.next() else {
            return json!({ "status": "error", "error": "missing-job-id" });
        };
        created.push(json!({
            "schemaVersion": JOB_SCHEMA_VERSION,
            "jobId": job_id,
            "policyId": policy_id,
            "backupId": backup_id,
            "primaryTargetId": primary_target_id,
            "replicaTargetId": replica_id,
            "mode": mode,
            "phase": "queued",
            "runId": run_id,
            "scheduleSlot": schedule_slot,
            "slotDigest": slot_digest,
            "objectSetDigest": object_set_digest,
            "controlObjectDigest": control_digest,
            "objects": objects,
            "primaryReceiptSnapshot": snapshot,
            "createdAt": now,
            "updatedAt": now,
            "error": null,
            "attempts": 0,
            "maxAttempts": DEFAULT_MAX_ATTEMPTS,
        }));
    }
    json!({ "jobs": created })
}

fn receipt_inventory(receipt: Option<&Value>) -> (String, String, Value) {
    if !matches!(receipt, Some(value) if python_truthy(value)) {
        return (String::new(), String::new(), json!([]));
    }
    let Some(map) = receipt.and_then(Value::as_object) else {
        return (String::new(), String::new(), json!([]));
    };
    let object_set_digest = first_str(&[map.get("objectSetDigest")]);
    let control_digest = first_str(&[map.get("controlObjectDigest"), map.get("objectDigest")]);
    let objects = match map.get("objects") {
        Some(Value::Array(items)) => Value::Array(items.clone()),
        _ => json!([]),
    };
    (object_set_digest, control_digest, objects)
}

fn receipt_snapshot(receipt: Option<&Value>) -> Map<String, Value> {
    let Some(Value::Object(map)) = receipt else {
        return Map::new();
    };
    let mut snapshot = Map::new();
    for key in RECEIPT_SNAPSHOT_KEYS {
        if let Some(value) = map.get(*key) {
            snapshot.insert((*key).to_string(), value.clone());
        }
    }
    snapshot
}

fn claim(case: &Value, now: &str) -> Value {
    let job_id = case.get("job_id").cloned().unwrap_or(Value::Null);
    let Some(job) = object_opt(case.get("job")) else {
        return json!({ "status": "error", "error": "not-found", "jobId": job_id });
    };
    if TERMINAL_PHASES.contains(&phase_text(&job).as_str()) {
        return json!({ "status": "terminal", "job": job });
    }
    let attempts = match python_int_or(job.get("attempts"), 0) {
        Ok(value) => match value.checked_add(1) {
            Some(value) => value,
            None => {
                return json!({
                    "status": "error",
                    "oracle_exception": "OverflowError",
                    "jobId": job_id,
                    "job": job,
                });
            }
        },
        Err(name) => {
            return json!({
                "status": "error",
                "oracle_exception": name,
                "jobId": job_id,
                "job": job,
            });
        }
    };
    let job = set_phase(
        job,
        json!("checking-target"),
        extra_from([("attempts", json!(attempts))]),
        now,
    );
    json!({ "status": "claimed", "job": job })
}

fn fail_job(case: &Value, now: &str) -> Value {
    let job = object_field(case, "job");
    let message = truncate_500(&python_str_or_empty(case.get("error")));
    let mode = match case.get("mode") {
        Some(value) => python_text(Some(value)),
        None => {
            let text = python_str_or_empty(job.get("mode"));
            if text.is_empty() {
                "required".to_string()
            } else {
                text
            }
        }
    };
    let attempts = match python_int_or(job.get("attempts"), 1) {
        Ok(value) => value,
        Err(name) => return json!({ "status": "error", "oracle_exception": name, "job": job }),
    };
    let max_attempts = match python_int_or(job.get("maxAttempts"), DEFAULT_MAX_ATTEMPTS) {
        Ok(value) => value,
        Err(name) => return json!({ "status": "error", "oracle_exception": name, "job": job }),
    };
    if mode == "required" {
        let folded = message.to_lowercase();
        if folded.contains("spool") && folded.contains("missing") {
            let job = set_phase(
                job,
                json!("repair-needed"),
                extra_from([
                    ("error", Value::String(message.clone())),
                    ("attempts", json!(attempts)),
                ]),
                now,
            );
            return json!({ "status": "repair-needed", "job": job });
        }
        if attempts < max_attempts {
            let current = parse_iso(Some(&Value::String(now.to_string()))).expect("frozen now");
            let next = format_iso(
                (current as f64 + compute_replication_backoff_seconds(attempts)).trunc() as i64,
            );
            let job = set_phase(
                job,
                json!("retry-wait"),
                extra_from([
                    ("error", Value::String(message.clone())),
                    ("attempts", json!(attempts)),
                    ("nextRetryAt", Value::String(next)),
                ]),
                now,
            );
            return json!({ "status": "retry-wait", "job": job });
        }
        let job = set_phase(
            job,
            json!("failed-terminal"),
            extra_from([
                ("error", Value::String(message.clone())),
                ("attempts", json!(attempts)),
            ]),
            now,
        );
        return json!({ "status": "failed-terminal", "job": job });
    }
    let job = set_phase(
        job,
        json!("failed"),
        extra_from([
            ("error", Value::String(message)),
            ("attempts", json!(attempts)),
        ]),
        now,
    );
    json!({ "status": "failed", "job": job })
}

fn classify_pending(case: &Value, now: &str) -> Value {
    let job = object_field(case, "job");
    let phase = phase_text(&job);
    if !ACTIVE_PHASES.contains(&phase.as_str()) && phase != "queued" {
        return json!({ "decision": "skip-inactive", "job": job });
    }
    if phase == "retry-wait" {
        let next_retry = job.get("nextRetryAt");
        if next_retry.is_some_and(python_truthy)
            && parse_iso(next_retry)
                .zip(parse_iso(Some(&Value::String(now.to_string()))))
                .is_some_and(|(parsed, current)| parsed > current)
        {
            return json!({ "decision": "skip-backoff", "job": job });
        }
    }
    json!({ "decision": "pending", "job": job })
}

fn has_open_required(case: &Value) -> Value {
    let policy_id = python_str_or_empty(case.get("policy_id"));
    let backup_id = case.get("backup_id");
    let slot_digest = case.get("slot_digest");
    for job in array_field(case, "jobs") {
        let Some(map) = job.as_object() else {
            continue;
        };
        if python_str_or_empty(map.get("jobId")).is_empty() {
            continue;
        }
        if python_str_or_empty(map.get("policyId")) != policy_id {
            continue;
        }
        if backup_id.is_some_and(python_truthy)
            && python_str_or_empty(map.get("backupId")) != python_str_or_empty(backup_id)
        {
            continue;
        }
        if python_str_or_empty(map.get("mode")) != "required" {
            continue;
        }
        if TERMINAL_PHASES.contains(&python_str_or_empty(map.get("phase")).as_str()) {
            continue;
        }
        if slot_digest.is_some_and(python_truthy) {
            let wanted = python_str_or_empty(slot_digest);
            let got = python_str_or_empty(map.get("slotDigest"));
            if !got.is_empty() && got != wanted {
                continue;
            }
        }
        return json!({ "open": true });
    }
    json!({ "open": false })
}

fn truncate_500(message: &str) -> String {
    message.chars().take(500).collect()
}

fn first_str(values: &[Option<&Value>]) -> String {
    for value in values.iter().flatten() {
        if python_truthy(value) {
            return python_text(Some(value));
        }
    }
    String::new()
}

fn json_number(seconds: f64) -> Value {
    if seconds.fract() == 0.0 && seconds.abs() <= i64::MAX as f64 {
        json!(seconds as i64)
    } else {
        json!(seconds)
    }
}
