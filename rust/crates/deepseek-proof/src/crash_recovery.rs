//! Read-only replay of legacy repair-worker crash/takeover Evidence.
//! These observations do not authorize an action or prove a provider effect occurred.

use serde_json::Value;
use std::cmp::Ordering;

use crate::legacy_values::{
    ParsedTimestamp, parse_timestamp, python_integer, python_text, require_fields,
    value_or_empty_text,
};

pub const CRASH_RECOVERY_PROOF_CHECKS: &[&str] = &[
    "crashRecoveryObservedExistingEffect",
    "leaseTakeoverUsedNewExecutionEpoch",
    "realWorkerCrashOccursDuringRemoteRepair",
    "freshWorkerTakesOverExpiredAction",
    "takeoverExecutionEpochStrictlyIncreases",
    "takeoverEntersReconcilingBeforeMutation",
    "takeoverFindsExistingRemoteEffect",
    "takeoverDoesNotCreateSecondRepairJob",
];

pub fn validate_crash_recovery_proof(evidence: &Value) -> Vec<String> {
    let Some(evidence) = evidence.as_object() else {
        return vec!["not-a-dict".to_string()];
    };
    let mut errors = require_fields(
        evidence,
        &[
            "actionId",
            "workerAPid",
            "workerBPid",
            "processAReturnCode",
            "epochA",
            "epochB",
            "repairId",
            "repairPhaseAtCrash",
            "reconciliationDirective",
            "workerALeaseUntil",
            "remoteRepairJobCountBefore",
            "remoteRepairJobCountAfter",
            "remoteRepairJobIdsBefore",
            "remoteRepairJobIdsAfter",
            "journalEvents",
        ],
    );
    let [
        Some(pid_a),
        Some(pid_b),
        Some(epoch_a),
        Some(epoch_b),
        Some(return_code),
        Some(before_count),
        Some(after_count),
    ] = [
        "workerAPid",
        "workerBPid",
        "epochA",
        "epochB",
        "processAReturnCode",
        "remoteRepairJobCountBefore",
        "remoteRepairJobCountAfter",
    ]
    .map(|field| python_integer(evidence.get(field)))
    else {
        errors.push("invalid-crash-takeover-numeric-fields".to_string());
        return errors;
    };
    if !pid_a.is_positive() || !pid_b.is_positive() || pid_a == pid_b {
        errors.push("worker-pids-not-distinct-positive".to_string());
    }
    if return_code.is_zero() {
        errors.push("worker-a-not-hard-terminated".to_string());
    }
    if epoch_b.compare(&epoch_a) != Ordering::Greater {
        errors.push("takeover-execution-epoch-not-increased".to_string());
    }
    if !before_count.is_one() || !after_count.is_one() {
        errors.push("underlying-repair-job-count-not-exactly-one".to_string());
    }
    let repair_id = value_or_empty_text(evidence.get("repairId"));
    let stable_ids = ["remoteRepairJobIdsBefore", "remoteRepairJobIdsAfter"]
        .iter()
        .all(|field| {
            evidence
                .get(*field)
                .and_then(Value::as_array)
                .is_some_and(|ids| ids.len() == 1 && python_text(ids.first()) == repair_id)
        });
    if !stable_ids || !before_count.is_one() || !after_count.is_one() {
        errors.push("underlying-repair-job-identity-not-stable".to_string());
    }
    let lease_expiry =
        match parse_timestamp(&value_or_empty_text(evidence.get("workerALeaseUntil"))) {
            ParsedTimestamp::Aware(instant) => Some(instant),
            // Python retains a naive datetime then raises TypeError during comparison.
            // The separately marked oracle-exception vector requires deterministic rejection.
            _ => {
                errors.push("invalid-worker-a-lease-expiry".to_string());
                None
            }
        };
    if !matches!(
        value_or_empty_text(evidence.get("repairPhaseAtCrash")).as_str(),
        "selecting-source"
            | "acquiring-source-hold"
            | "validating-source-control"
            | "scanning-destination"
            | "transferring-components"
            | "verifying-components"
            | "finalizing"
    ) {
        errors.push("worker-a-not-killed-during-active-repair".to_string());
    }
    if !matches!(
        value_or_empty_text(evidence.get("reconciliationDirective")).as_str(),
        "RESUME_EXECUTION" | "ADVANCE_TO_VERIFYING"
    ) {
        errors.push("invalid-reconciliation-directive".to_string());
    }
    let Some(events) = evidence.get("journalEvents").and_then(Value::as_array) else {
        errors.push("journal-events-must-be-list".to_string());
        return errors;
    };
    let mut executing_index = None;
    let mut reconciling_index = None;
    let mut takeover_at = None;
    let pid_a_text = pid_a.canonical_text();
    let pid_b_text = pid_b.canonical_text();
    for (index, event) in events.iter().enumerate() {
        let Some(event) = event.as_object() else {
            continue;
        };
        let Some(event_epoch) = python_integer(event.get("executionEpoch")) else {
            continue;
        };
        let Some(effect) = event.get("effectHandle").and_then(Value::as_object) else {
            continue;
        };
        if value_or_empty_text(effect.get("kind")) != "repair"
            || value_or_empty_text(effect.get("repairId")) != repair_id
        {
            continue;
        }
        let owner = value_or_empty_text(event.get("ownerInstanceId"));
        let state = value_or_empty_text(event.get("state"));
        // Preserve the legacy last-matching-event and PID-substring behavior.
        if state == "EXECUTING" && event_epoch == epoch_a && owner.contains(&pid_a_text) {
            executing_index = Some(index);
        }
        if state == "RECONCILING"
            && value_or_empty_text(event.get("eventType")) == "ACTION_TAKEOVER"
            && event_epoch == epoch_b
            && owner.contains(&pid_b_text)
        {
            reconciling_index = Some(index);
            match parse_timestamp(&value_or_empty_text(event.get("createdAt"))) {
                ParsedTimestamp::Aware(instant) => takeover_at = Some(instant),
                ParsedTimestamp::Invalid => takeover_at = None,
                // Legacy code preserves an earlier aware timestamp on a later naive event.
                ParsedTimestamp::Naive => {}
            }
        }
    }
    if executing_index.is_none() {
        errors.push("missing-worker-a-executing-effect-event".to_string());
    }
    if reconciling_index.is_none() {
        errors.push("missing-worker-b-reconciling-event".to_string());
    } else if takeover_at.is_none() {
        errors.push("missing-worker-b-takeover-timestamp".to_string());
    }
    if matches!((executing_index, reconciling_index), (Some(executing), Some(reconciling)) if reconciling <= executing)
    {
        errors.push("reconciling-event-not-after-executing-event".to_string());
    }
    if matches!((lease_expiry, takeover_at), (Some(lease), Some(takeover)) if takeover <= lease) {
        errors.push("takeover-occurred-before-worker-a-lease-expiry".to_string());
    }
    errors
}
