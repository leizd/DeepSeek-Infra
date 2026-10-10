//! Compatibility checks for blast-radius and atomic budget observations.
//! These pure validators neither simulate a wave nor admit a control-plane action.

use serde_json::{Value, json};
use std::cmp::Ordering;
use std::collections::HashSet;

use crate::legacy_values::{
    PythonInteger, python_equal, python_integer, require_fields, value_or_empty_text,
};

pub const SAFETY_ADMISSION_PROOF_CHECKS: &[&str] = &[
    "blastRadiusInvariantVerified",
    "degradedFleetCannotBeFurtherDegraded",
    "runningEffectsParticipateInBlastRadiusSimulation",
    "atomicBudgetAdmissionVerified",
    "twoProcessesCannotOversubscribeGlobalBudget",
    "twoProcessesCannotOversubscribeTargetBudget",
    "twoProcessesCannotOversubscribePolicyBudget",
    "twoProcessesCannotOversubscribeFailureDomainBudget",
];

pub fn validate_safety_admission_check(check_name: &str, evidence: &Value) -> Vec<String> {
    if SAFETY_ADMISSION_PROOF_CHECKS[..3].contains(&check_name) {
        validate_blast_radius_proof(evidence, check_name)
    } else if SAFETY_ADMISSION_PROOF_CHECKS[3..].contains(&check_name) {
        validate_atomic_budget_proof(evidence, check_name)
    } else {
        vec![format!("unsupported-check:{check_name}")]
    }
}

pub fn validate_blast_radius_proof(evidence: &Value, check_name: &str) -> Vec<String> {
    let Some(evidence) = evidence.as_object() else {
        return vec!["not-a-dict".to_string()];
    };
    let mut errors = require_fields(
        evidence,
        &[
            "simulator",
            "simulationPassed",
            "proposedActionIds",
            "simulationDetails",
        ],
    );
    if evidence.get("simulator").and_then(Value::as_str)
        != Some("resilience_coordinator.simulate_coordination_wave")
    {
        errors.push("blast-radius-simulator-identity-mismatch".to_string());
    }
    if evidence.get("simulationPassed") != Some(&Value::Bool(true)) {
        errors.push("blast-radius-simulation-not-passed".to_string());
    }
    let empty = json!([]);
    let proposed = match evidence.get("proposedActionIds") {
        Some(value @ Value::Array(_)) => value,
        _ => {
            errors.push("blast-radius-proposed-actions-must-be-list".to_string());
            &empty
        }
    };
    let Some(details) = evidence.get("simulationDetails").and_then(Value::as_object) else {
        errors.push("blast-radius-simulation-details-must-be-object".to_string());
        return errors;
    };
    if details.get("passed") != Some(&Value::Bool(true)) {
        errors.push("blast-radius-details-not-passed".to_string());
    }
    if !python_equal(
        details.get("proposedActionIds").unwrap_or(&Value::Null),
        proposed,
    ) {
        errors.push("blast-radius-proposed-action-binding-mismatch".to_string());
    }
    let has_running_ids = match details.get("runningActionIds").and_then(Value::as_array) {
        Some(ids) => !ids.is_empty(),
        None => {
            errors.push("blast-radius-running-actions-must-be-list".to_string());
            false
        }
    };
    let Some(evaluations) = details
        .get("evaluations")
        .and_then(Value::as_object)
        .filter(|items| !items.is_empty())
    else {
        errors.push("blast-radius-evaluations-missing".to_string());
        return errors;
    };
    let mut has_running_effects = false;
    for (key, evaluation) in evaluations {
        let Some(evaluation) = evaluation.as_object() else {
            errors.push(format!("blast-radius-evaluation-not-object:{key}"));
            continue;
        };
        errors.extend(
            require_fields(
                evaluation,
                &[
                    "policyId",
                    "backupId",
                    "minCommittedCopies",
                    "minFailureDomains",
                    "copiesBefore",
                    "copiesDuring",
                    "copySafetyFloor",
                    "failureDomainsBefore",
                    "failureDomainsDuring",
                    "failureDomainSafetyFloor",
                    "runningEffectCount",
                    "passed",
                ],
            )
            .into_iter()
            .map(|error| format!("blast-radius-{key}-{error}")),
        );
        let [
            Some(min_copies),
            Some(min_domains),
            Some(copies_before),
            Some(copies_during),
            Some(copy_floor),
            Some(domain_floor),
            Some(running_effect_count),
        ] = [
            "minCommittedCopies",
            "minFailureDomains",
            "copiesBefore",
            "copiesDuring",
            "copySafetyFloor",
            "failureDomainSafetyFloor",
            "runningEffectCount",
        ]
        .map(|field| python_integer(evaluation.get(field)))
        else {
            errors.push(format!("blast-radius-invalid-numeric-fields:{key}"));
            continue;
        };
        let expected_copy_floor = if copies_before.compare(&min_copies) != Ordering::Less {
            &min_copies
        } else {
            &copies_before
        };
        if &copy_floor != expected_copy_floor
            || copies_during.compare(&copy_floor) == Ordering::Less
        {
            errors.push(format!("blast-radius-copy-floor-violation:{key}"));
        }
        match (
            evaluation
                .get("failureDomainsBefore")
                .and_then(Value::as_array),
            evaluation
                .get("failureDomainsDuring")
                .and_then(Value::as_array),
        ) {
            (Some(before), Some(during)) => {
                let before_count = PythonInteger::from_usize(before.len());
                let during_count = PythonInteger::from_usize(during.len());
                let expected_domain_floor = if before_count.compare(&min_domains) != Ordering::Less
                {
                    &min_domains
                } else {
                    &before_count
                };
                if &domain_floor != expected_domain_floor
                    || during_count.compare(&domain_floor) == Ordering::Less
                {
                    errors.push(format!("blast-radius-domain-floor-violation:{key}"));
                }
            }
            _ => errors.push(format!("blast-radius-failure-domains-must-be-lists:{key}")),
        }
        if running_effect_count.is_negative() {
            errors.push(format!("blast-radius-negative-running-effect-count:{key}"));
        }
        // Only sum(max(0, count)) >= 1 is observed; a boolean avoids bigint addition/overflow.
        has_running_effects |= running_effect_count.is_positive();
        if evaluation.get("passed") != Some(&Value::Bool(true)) {
            errors.push(format!("blast-radius-evaluation-not-passed:{key}"));
        }
    }
    if check_name == "runningEffectsParticipateInBlastRadiusSimulation"
        && (!has_running_ids || !has_running_effects)
    {
        errors.push("blast-radius-running-effects-not-proven".to_string());
    }
    errors
}

pub fn validate_atomic_budget_proof(evidence: &Value, check_name: &str) -> Vec<String> {
    let Some(evidence) = evidence.as_object() else {
        return vec!["not-a-dict".to_string()];
    };
    let mut errors = require_fields(
        evidence,
        &["scope", "processResults", "admittedCount", "rejectedCount"],
    );
    let expected_scope = match check_name {
        "twoProcessesCannotOversubscribeGlobalBudget" => Some("global"),
        "twoProcessesCannotOversubscribeTargetBudget" => Some("target"),
        "twoProcessesCannotOversubscribePolicyBudget" => Some("policy"),
        "twoProcessesCannotOversubscribeFailureDomainBudget" => Some("failure-domain"),
        _ => None,
    };
    let scope = value_or_empty_text(evidence.get("scope"));
    if let Some(expected) = expected_scope.filter(|expected| *expected != scope) {
        errors.push(format!("atomic-budget-scope-mismatch:{scope}!={expected}"));
    } else if !matches!(
        scope.as_str(),
        "global" | "target" | "policy" | "failure-domain"
    ) {
        errors.push("invalid-atomic-budget-scope".to_string());
    }
    let Some(results) = evidence.get("processResults").and_then(Value::as_array) else {
        errors.push("process-results-must-be-list".to_string());
        return errors;
    };
    if results.len() != 2 {
        errors.push("atomic-budget-proof-requires-two-process-results".to_string());
    }
    let mut pids = HashSet::new();
    let mut admitted = 0_usize;
    let mut rejected = 0_usize;
    for result in results {
        let Some(result) = result.as_object() else {
            errors.push("process-result-must-be-object".to_string());
            continue;
        };
        let Some(pid) = python_integer(result.get("pid")) else {
            errors.push("invalid-process-pid".to_string());
            continue;
        };
        if !pid.is_positive() {
            errors.push("invalid-process-pid".to_string());
        }
        pids.insert(pid.canonical_text());
        match result.get("admitted") {
            Some(Value::Bool(true)) => {
                admitted += 1;
                if !python_integer(result.get("executionEpoch"))
                    .is_some_and(|epoch| epoch.is_positive())
                {
                    errors.push("admitted-process-missing-execution-epoch".to_string());
                }
            }
            Some(Value::Bool(false)) => {
                rejected += 1;
                if value_or_empty_text(result.get("reason")).is_empty() {
                    errors.push("rejected-process-missing-reason".to_string());
                }
            }
            _ => errors.push("process-result-admitted-must-be-boolean".to_string()),
        }
    }
    if pids.len() != 2 {
        errors.push("atomic-budget-process-pids-not-distinct".to_string());
    }
    if admitted != 1 || rejected != 1 {
        errors.push("atomic-budget-race-not-one-admitted-one-rejected".to_string());
    }
    if !python_equal(
        evidence.get("admittedCount").unwrap_or(&Value::Null),
        &json!(admitted),
    ) || !python_equal(
        evidence.get("rejectedCount").unwrap_or(&Value::Null),
        &json!(rejected),
    ) {
        errors.push("atomic-budget-declared-counts-mismatch".to_string());
    }
    errors
}
