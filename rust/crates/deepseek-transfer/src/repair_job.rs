//! Replica-repair durable phase-machine semantics.
//! These functions do not contact a provider or move payload bytes.

use crate::pycompat::{
    array_field, extra_from, extra_map, format_iso, object_field, object_opt, parse_iso,
    phase_text, python_int, python_int_or, python_str_or_empty, python_text, python_truthy,
    set_phase,
};
use serde_json::{Map, Value, json};

const REPAIR_SCHEMA_VERSION: i64 = 2;
const REBALANCE_SCHEMA_VERSION: i64 = 1;
const DEFAULT_MAX_ATTEMPTS: i64 = 5;
const DEFAULT_TRAFFIC_CLASS: i64 = 2;
const DEFAULT_CANCEL_REASON: &str = "resilience-action-compensation";
const DEFAULT_REBALANCE_REASON: &str = "failure-domain-rebalance";
const BACKOFF_SECONDS: [i64; 5] = [5, 15, 45, 120, 300];
const REPAIR_TERMINAL_PHASES: &[&str] = &[
    "healthy",
    "failed-terminal",
    "failed",
    "quarantined",
    "superseded",
    "skipped",
    "cancelled",
];
const REPAIR_ACTIVE_PHASES: &[&str] = &[
    "queued",
    "selecting-source",
    "acquiring-source-hold",
    "validating-source-control",
    "scanning-destination",
    "transferring-components",
    "verifying-components",
    "finalizing",
    "retry-wait",
];
const REBALANCE_CREATE_TERMINAL: &[&str] = &["complete", "failed", "cancelled"];

pub fn replay_repair_phase_case(case: &Value, now: &str) -> Value {
    let op = case.get("op").and_then(Value::as_str).unwrap_or_default();
    match op {
        "backoff" => {
            json!({ "seconds": compute_repair_backoff_seconds(case_i64(case, "attempt")) })
        }
        "set-repair-phase" | "set-rebalance-phase" => json!({
            "job": set_phase(object_field(case, "job"), case.get("phase").cloned().unwrap_or(Value::Null), extra_map(case), now)
        }),
        "create-repair" => create_repair(case, now),
        "create-rebalance" => create_rebalance(case, now),
        "cancel-repair" => cancel_repair(case, now),
        "cancel-rebalance" => cancel_rebalance(case, now),
        "claim-repair" => claim_repair(case, now),
        "classify-pending" => classify_pending(case, now),
        "route-error" => route_error(case, now),
        _ => json!({ "status": "error", "error": format!("unknown-op:{op}") }),
    }
}

pub fn compute_repair_backoff_seconds(attempt: i64) -> i64 {
    let index = attempt
        .saturating_sub(1)
        .clamp(0, (BACKOFF_SECONDS.len() - 1) as i64) as usize;
    BACKOFF_SECONDS[index]
}

fn create_repair(case: &Value, now: &str) -> Value {
    let fields = object_field(case, "fields");
    let action = fields
        .get("resilienceActionId")
        .cloned()
        .unwrap_or(Value::Null);
    if python_truthy(&action) {
        for job in array_field(case, "existing_jobs") {
            if job.get("resilienceActionId") == Some(&action) {
                return json!({ "status": "existing", "job": job.clone() });
            }
        }
    }
    let traffic = match fields.get("trafficClass") {
        None => DEFAULT_TRAFFIC_CLASS,
        Some(value) => match python_int(value) {
            Ok(value) => value,
            Err(name) => return json!({ "status": "error", "oracle_exception": name }),
        },
    };
    json!({
        "status": "created",
        "job": {
            "schemaVersion": REPAIR_SCHEMA_VERSION,
            "repairId": fields.get("repairId").cloned().unwrap_or(Value::Null),
            "resilienceActionId": action,
            "policyId": fields.get("policyId").cloned().unwrap_or(Value::Null),
            "backupId": fields.get("backupId").cloned().unwrap_or(Value::Null),
            "sourceTargetId": fields.get("sourceTargetId").cloned().unwrap_or(Value::Null),
            "destTargetId": fields.get("destTargetId").cloned().unwrap_or(Value::Null),
            "objectSetDigest": fields.get("objectSetDigest").cloned().unwrap_or(Value::Null),
            "repairMode": "auto",
            "trafficClass": traffic,
            "phase": "queued",
            "components": {},
            "bytesRepaired": 0,
            "attempt": 0,
            "maxAttempts": DEFAULT_MAX_ATTEMPTS,
            "nextAttemptAt": null,
            "holdId": null,
            "createdAt": now,
            "updatedAt": now,
            "error": null,
        }
    })
}

fn create_rebalance(case: &Value, now: &str) -> Value {
    let fields = object_field(case, "fields");
    let action = fields
        .get("resilienceActionId")
        .cloned()
        .unwrap_or(Value::Null);
    for job in array_field(case, "existing_jobs") {
        if python_truthy(&action) && job.get("resilienceActionId") == Some(&action) {
            return json!({ "status": "existing", "job": job.clone() });
        }
        let phase = job.get("phase");
        let terminal = matches!(phase, Some(Value::String(value)) if REBALANCE_CREATE_TERMINAL.contains(&value.as_str()));
        if !terminal {
            return json!({ "status": "existing", "job": job.clone() });
        }
    }
    let reason = match fields.get("reason") {
        Some(Value::String(value)) => Value::String(value.clone()),
        Some(value) => value.clone(),
        None => Value::String(DEFAULT_REBALANCE_REASON.to_string()),
    };
    let prune = match fields.get("pruneSourceAfter") {
        Some(value) => value.clone(),
        None => Value::Bool(false),
    };
    json!({
        "status": "created",
        "job": {
            "schemaVersion": REBALANCE_SCHEMA_VERSION,
            "jobId": fields.get("jobId").cloned().unwrap_or(Value::Null),
            "resilienceActionId": action,
            "policyId": fields.get("policyId").cloned().unwrap_or(Value::Null),
            "backupId": fields.get("backupId").cloned().unwrap_or(Value::Null),
            "sourceTargetId": fields.get("sourceTargetId").cloned().unwrap_or(Value::Null),
            "destTargetId": fields.get("destTargetId").cloned().unwrap_or(Value::Null),
            "reason": reason,
            "pruneSourceAfter": prune,
            "phase": "pending",
            "bytesTransferred": 0,
            "createdAt": now,
            "updatedAt": now,
        }
    })
}

fn cancel_repair(case: &Value, now: &str) -> Value {
    let repair_id = string_field(case, "repair_id");
    let Some(job) = object_opt(case.get("job")) else {
        return json!({
            "status": "unknown",
            "repairId": repair_id,
            "reason": "repair-job-not-found",
        });
    };
    let phase = phase_text(&job);
    if phase == "cancelled" {
        return json!({
            "status": "cancelled",
            "repairId": repair_id,
            "phase": phase,
            "job": job,
        });
    }
    if phase != "queued" {
        return json!({
            "status": "not-cancelable",
            "repairId": repair_id,
            "phase": phase,
            "job": job,
        });
    }
    let reason = optional_reason(case);
    let cancelled = set_phase(
        job,
        json!("cancelled"),
        extra_from([
            ("cancellationReason", Value::String(reason)),
            ("cancelledAt", Value::String(now.to_string())),
        ]),
        now,
    );
    if !observed_cancelled(case, &cancelled) {
        return json!({
            "status": "unknown",
            "repairId": repair_id,
            "reason": "repair-cancellation-not-observed",
        });
    }
    json!({
        "status": "cancelled",
        "repairId": repair_id,
        "phase": "cancelled",
        "job": cancelled,
    })
}

fn cancel_rebalance(case: &Value, now: &str) -> Value {
    let job_id = string_field(case, "job_id");
    let Some(job) = object_opt(case.get("job")) else {
        return json!({
            "status": "unknown",
            "jobId": job_id,
            "reason": "rebalance-job-not-found",
        });
    };
    let phase = phase_text(&job);
    if phase == "cancelled" {
        return json!({
            "status": "cancelled",
            "jobId": job_id,
            "phase": phase,
            "job": job,
        });
    }
    if phase != "pending" {
        return json!({
            "status": "not-cancelable",
            "jobId": job_id,
            "phase": phase,
            "job": job,
        });
    }
    let reason = optional_reason(case);
    let cancelled = set_phase(
        job,
        json!("cancelled"),
        extra_from([
            ("cancellationReason", Value::String(reason)),
            ("cancelledAt", Value::String(now.to_string())),
        ]),
        now,
    );
    if !observed_cancelled(case, &cancelled) {
        return json!({
            "status": "unknown",
            "jobId": job_id,
            "reason": "rebalance-cancellation-not-observed",
        });
    }
    json!({
        "status": "cancelled",
        "jobId": job_id,
        "phase": "cancelled",
        "job": cancelled,
    })
}

fn claim_repair(case: &Value, now: &str) -> Value {
    let repair_id = case.get("repair_id").cloned().unwrap_or(Value::Null);
    let Some(job) = object_opt(case.get("job")) else {
        return json!({ "status": "error", "error": "not-found", "repairId": repair_id });
    };
    if REPAIR_TERMINAL_PHASES.contains(&phase_text(&job).as_str()) {
        let status = if job.get("phase") == Some(&json!("healthy")) {
            Value::String("success".to_string())
        } else {
            Value::String(python_text(job.get("phase")))
        };
        return json!({ "status": status, "repairId": repair_id, "job": job });
    }
    let attempt = match python_int_or(job.get("attempt"), 0) {
        Ok(value) => match value.checked_add(1) {
            Some(value) => value,
            None => {
                return json!({
                    "status": "error",
                    "oracle_exception": "OverflowError",
                    "repairId": repair_id,
                    "job": job,
                });
            }
        },
        Err(name) => {
            return json!({
                "status": "error",
                "oracle_exception": name,
                "repairId": repair_id,
                "job": job,
            });
        }
    };
    let max_attempts = match python_int_or(job.get("maxAttempts"), DEFAULT_MAX_ATTEMPTS) {
        Ok(value) => value,
        Err(name) => {
            return json!({
                "status": "error",
                "oracle_exception": name,
                "repairId": repair_id,
                "job": job,
            });
        }
    };
    if attempt > max_attempts {
        let job = set_phase(
            job,
            json!("failed-terminal"),
            extra_from([
                ("error", json!("max-attempts-exceeded")),
                ("attempt", json!(attempt)),
            ]),
            now,
        );
        return json!({
            "status": "error",
            "error": "max-attempts-exceeded",
            "repairId": repair_id,
            "job": job,
        });
    }
    let job = set_phase(
        job,
        json!("selecting-source"),
        extra_from([("attempt", json!(attempt))]),
        now,
    );
    json!({ "status": "claimed", "repairId": repair_id, "job": job })
}

fn classify_pending(case: &Value, now: &str) -> Value {
    let job = object_field(case, "job");
    let phase = phase_text(&job);
    if !REPAIR_ACTIVE_PHASES.contains(&phase.as_str()) && phase != "queued" {
        return json!({ "decision": "skip-inactive", "job": job });
    }
    let next_at = parse_iso(job.get("nextAttemptAt"));
    let current = parse_iso(Some(&Value::String(now.to_string())));
    if let (Some(next_at), Some(current)) = (next_at, current) {
        if current < next_at {
            return json!({ "decision": "skip-backoff", "job": job });
        }
    }
    let max_attempts = match python_int_or(job.get("maxAttempts"), DEFAULT_MAX_ATTEMPTS) {
        Ok(value) => value,
        Err(name) => return json!({ "decision": "error", "oracle_exception": name, "job": job }),
    };
    let attempt = match python_int_or(job.get("attempt"), 0) {
        Ok(value) => value,
        Err(name) => return json!({ "decision": "error", "oracle_exception": name, "job": job }),
    };
    if attempt >= max_attempts && matches!(phase.as_str(), "retry-wait" | "queued") {
        let job = set_phase(
            job,
            json!("failed-terminal"),
            extra_from([("error", json!("max-attempts-exceeded"))]),
            now,
        );
        return json!({ "decision": "fail-max-attempts", "job": job });
    }
    json!({ "decision": "pending", "job": job })
}

fn route_error(case: &Value, now: &str) -> Value {
    let job = object_field(case, "job");
    let err_msg = python_str_or_empty(case.get("error"));
    let kind = case
        .get("kind")
        .and_then(Value::as_str)
        .unwrap_or("generic");
    let attempt = match python_int_or(job.get("attempt"), 0) {
        Ok(value) => value,
        Err(name) => return json!({ "status": "error", "oracle_exception": name, "job": job }),
    };
    let max_attempts = match python_int_or(job.get("maxAttempts"), DEFAULT_MAX_ATTEMPTS) {
        Ok(value) => value,
        Err(name) => return json!({ "status": "error", "oracle_exception": name, "job": job }),
    };
    if kind == "lease-lost" {
        return retry_wait(job, &err_msg, attempt, now);
    }
    let folded = err_msg.to_lowercase();
    if folded.contains("source component corrupt") {
        let job = set_phase(
            job,
            json!("selecting-source"),
            extra_from([
                ("sourceTargetId", Value::Null),
                ("error", Value::String(err_msg)),
            ]),
            now,
        );
        return json!({ "status": "selecting-source", "job": job });
    }
    if folded.contains("cas mismatch") {
        let job = set_phase(
            job,
            json!("scanning-destination"),
            extra_from([("error", Value::String(err_msg))]),
            now,
        );
        return json!({ "status": "scanning-destination", "job": job });
    }
    if attempt >= max_attempts {
        let job = set_phase(
            job,
            json!("failed-terminal"),
            extra_from([("error", Value::String(err_msg))]),
            now,
        );
        return json!({ "status": "failed-terminal", "job": job });
    }
    retry_wait(job, &err_msg, attempt, now)
}

fn retry_wait(job: Map<String, Value>, err_msg: &str, attempt: i64, now: &str) -> Value {
    let current = parse_iso(Some(&Value::String(now.to_string()))).expect("frozen now");
    let next_at = format_iso(current + compute_repair_backoff_seconds(attempt));
    let job = set_phase(
        job,
        json!("retry-wait"),
        extra_from([
            ("error", Value::String(err_msg.to_string())),
            ("nextAttemptAt", Value::String(next_at)),
        ]),
        now,
    );
    json!({ "status": "retry-wait", "job": job })
}

fn observed_cancelled(case: &Value, cancelled: &Map<String, Value>) -> bool {
    match case.get("observed") {
        None => phase_text(cancelled) == "cancelled",
        Some(value) if !python_truthy(value) => false,
        Some(Value::Object(observed)) => phase_text(observed) == "cancelled",
        Some(_) => false,
    }
}

fn optional_reason(case: &Value) -> String {
    match case.get("reason") {
        Some(Value::String(value)) => value.clone(),
        Some(value) => python_text(Some(value)),
        None => DEFAULT_CANCEL_REASON.to_string(),
    }
}

fn string_field(case: &Value, key: &str) -> String {
    python_text(case.get(key))
}

fn case_i64(case: &Value, key: &str) -> i64 {
    python_int(case.get(key).unwrap_or(&Value::Null)).unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::compute_repair_backoff_seconds;

    #[test]
    fn backoff_table_matches_python() {
        assert_eq!(compute_repair_backoff_seconds(0), 5);
        assert_eq!(compute_repair_backoff_seconds(1), 5);
        assert_eq!(compute_repair_backoff_seconds(2), 15);
        assert_eq!(compute_repair_backoff_seconds(3), 45);
        assert_eq!(compute_repair_backoff_seconds(4), 120);
        assert_eq!(compute_repair_backoff_seconds(5), 300);
        assert_eq!(compute_repair_backoff_seconds(100), 300);
        assert_eq!(compute_repair_backoff_seconds(-3), 5);
    }
}
