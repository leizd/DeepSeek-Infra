//! Legacy control observations and byte-bound copy proofs. Validation does not schedule,
//! authorize, or perform a mutation; Go owns the eventual control decisions and state.

use crate::legacy_values::{is_plain_sha256, missing, python_text, python_truthy, require_fields};
use crate::storage_evidence::validate_autonomous_storage_bytes_proof;
use serde_json::{Map, Value};

pub const CONTROL_STORAGE_EVIDENCE_CHECKS: &[&str] = &[
    "retentionSafetyProof",
    "authorityRetentionSafety",
    "decisionProof",
    "resilienceDecisionProof",
    "resilienceScoreProof",
    "resilienceSnapshotProof",
    "realReplicaTransferUsesEndpointAAndB",
    "realRebalanceUsesEndpointAAndC",
];

pub fn validate_control_storage_evidence_check(check_name: &str, evidence: &Value) -> Vec<String> {
    match check_name {
        "retentionSafetyProof" | "authorityRetentionSafety" => {
            validate_retention_safety_proof(evidence)
        }
        "decisionProof" | "resilienceDecisionProof" => validate_decision_proof(evidence),
        "resilienceScoreProof" | "resilienceSnapshotProof" => validate_resilience_proof(evidence),
        "realReplicaTransferUsesEndpointAAndB" => validate_autonomous_repair_proof(evidence),
        "realRebalanceUsesEndpointAAndC" => validate_autonomous_rebalance_proof(evidence),
        _ => vec![format!("unsupported-check:{check_name}")],
    }
}

pub fn validate_retention_safety_proof(value: &Value) -> Vec<String> {
    let Some(evidence) = value.as_object() else {
        return vec!["not-a-dict".to_string()];
    };
    let safety = nested_or_root(evidence, "retentionSafety");
    let fields = [
        "checkpointVerified",
        "ancestorCoverage",
        "replicaAgreement",
        "dependencyClosure",
    ];
    let mut errors = require_fields(safety, &fields);
    for field in fields {
        if safety.get(field) != Some(&Value::Bool(true)) {
            errors.push(format!("retention-safety-{field}-not-true"));
        }
    }
    errors
}

pub fn validate_decision_proof(value: &Value) -> Vec<String> {
    let Some(evidence) = value.as_object() else {
        return vec!["not-a-dict".to_string()];
    };
    let decision = nested_or_root(evidence, "decisionProof");
    let mut errors = require_fields(
        decision,
        &[
            "riskDigest",
            "policyVersion",
            "actionAllowed",
            "simulationPassed",
            "executionVerified",
        ],
    );
    for field in ["riskDigest", "riskBeforeDigest", "riskAfterDigest"] {
        if !missing(decision.get(field)) && !is_plain_sha256(decision.get(field)) {
            errors.push(format!("invalid-sha256:{field}"));
        }
    }
    for field in ["actionAllowed", "simulationPassed", "executionVerified"] {
        if decision.get(field) != Some(&Value::Bool(true)) {
            errors.push(format!("decision-{field}-not-true"));
        }
    }
    if decision.contains_key("effectObserved")
        && decision.get("effectObserved") != Some(&Value::Bool(true))
    {
        errors.push("decision-effectObserved-not-true".to_string());
    }
    errors
}

pub fn validate_resilience_proof(value: &Value) -> Vec<String> {
    let Some(evidence) = value.as_object() else {
        return vec!["not-a-dict".to_string()];
    };
    let mut errors = require_fields(evidence, &["riskDigest", "score", "overallRisk"]);
    if !missing(evidence.get("riskDigest")) && !is_plain_sha256(evidence.get("riskDigest")) {
        errors.push("invalid-sha256:riskDigest".to_string());
    }
    if evidence
        .get("score")
        .filter(|score| !score.is_null())
        .is_some_and(|score| {
            !score
                .as_f64()
                .is_some_and(|number| (0.0..=100.0).contains(&number))
        })
    {
        errors.push("invalid-resilience-score".to_string());
    }
    errors
}

pub fn validate_autonomous_repair_proof(evidence: &Value) -> Vec<String> {
    validate_copy_endpoint_proof(evidence, "endpointB", "destination-endpoint-b-mismatch")
}

pub fn validate_autonomous_rebalance_proof(evidence: &Value) -> Vec<String> {
    validate_copy_endpoint_proof(evidence, "endpointC", "destination-endpoint-c-mismatch")
}

fn validate_copy_endpoint_proof(
    value: &Value,
    destination_field: &str,
    mismatch: &str,
) -> Vec<String> {
    let Some(evidence) = value.as_object() else {
        return vec!["not-a-dict".to_string()];
    };
    let mut errors = validate_autonomous_storage_bytes_proof(value);
    errors.extend(require_fields(evidence, &["endpointA", destination_field]));
    let endpoint = evidence.get("endpoint");
    let destination = evidence.get(destination_field);
    if endpoint.is_some_and(python_truthy)
        && destination.is_some_and(python_truthy)
        && python_text(endpoint).trim_end_matches('/')
            != python_text(destination).trim_end_matches('/')
    {
        errors.push(mismatch.to_string());
    }
    errors
}

fn nested_or_root<'a>(evidence: &'a Map<String, Value>, field: &str) -> &'a Map<String, Value> {
    evidence
        .get(field)
        .and_then(Value::as_object)
        .unwrap_or(evidence)
}
