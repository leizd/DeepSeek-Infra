//! Read-only compatibility validators for the legacy recovery Evidence claims.
//!
//! These functions validate a producer's structured observations. They do not run a restore,
//! contact MinIO, inspect a process, or make provider execution authoritative.

use serde_json::Value;
use std::cmp::Ordering;
use std::collections::HashSet;

use crate::legacy_values::{
    PythonInteger, casefold, is_plain_sha256, missing, python_text, python_truthy, require_fields,
    value_or_empty_text,
};

pub const RESTORE_PROOF_CHECKS: &[&str] = &[
    "realPreDisasterBackupIsActuallyRestored",
    "realFreshProcessRestoresPreDisasterBackup",
    "restoredWorkspaceDigestMatchesPreDisasterDigest",
];
pub const BACKUP_COMMIT_PROOF_CHECKS: &[&str] = &[
    "realPostRecoveryBackupHasValidCommit",
    "realFreshProcessCreatesPostRecoveryBackup",
    "realPostRecoveryBackupHasValidReceiptBinding",
];
pub const DISTINCT_PID_PROOF_CHECKS: &[&str] = &["freshProcessAAndBHaveDifferentPids"];
pub const SIGKILL_PROOF_CHECKS: &[&str] = &[
    "processAIsDeadBeforeProcessBStarts",
    "processAExitedBySigkill",
];
pub const EPOCH_INCREASE_PROOF_CHECKS: &[&str] = &["realFreshProcessBootEpochStrictlyIncreases"];
pub const MINIO_ENDPOINTS_PROOF_CHECKS: &[&str] = &[
    "realThreeMinioProcessReplacementE2E",
    "realThreeMinioFreshProcessAuthorityRecoveryE2E",
    "realThreeMinioAutonomousRepairE2E",
    "realThreeMinioAutonomousRebalanceE2E",
    "realThreeMinioPredictivePlanningE2E",
];
pub const SCHEMA_ONLY_PROOF_CHECKS: &[&str] = &["evidenceCheckCannotPassWithoutStructuredProof"];

pub const RECOVERY_EVIDENCE_CHECKS: &[&str] = &[
    "realPreDisasterBackupIsActuallyRestored",
    "realFreshProcessRestoresPreDisasterBackup",
    "restoredWorkspaceDigestMatchesPreDisasterDigest",
    "realPostRecoveryBackupHasValidCommit",
    "realFreshProcessCreatesPostRecoveryBackup",
    "realPostRecoveryBackupHasValidReceiptBinding",
    "freshProcessAAndBHaveDifferentPids",
    "processAIsDeadBeforeProcessBStarts",
    "processAExitedBySigkill",
    "realFreshProcessBootEpochStrictlyIncreases",
    "realThreeMinioProcessReplacementE2E",
    "realThreeMinioFreshProcessAuthorityRecoveryE2E",
    "realThreeMinioAutonomousRepairE2E",
    "realThreeMinioAutonomousRebalanceE2E",
    "realThreeMinioPredictivePlanningE2E",
    "evidenceCheckCannotPassWithoutStructuredProof",
];

const RESTORE_FIELDS: &[&str] = &[
    "backupId",
    "targetId",
    "restoreId",
    "preBackupWorkspaceDigest",
    "corruptedWorkspaceDigest",
    "postRestoreWorkspaceDigest",
];
const BACKUP_COMMIT_FIELDS: &[&str] = &[
    "backupId",
    "commitKey",
    "receiptKey",
    "receiptDigest",
    "objectSetDigest",
];

pub fn validate_recovery_evidence_check(check_name: &str, evidence: &Value) -> Vec<String> {
    if RESTORE_PROOF_CHECKS.contains(&check_name) {
        return validate_restore_proof(evidence);
    }
    if BACKUP_COMMIT_PROOF_CHECKS.contains(&check_name) {
        return validate_backup_commit_proof(evidence);
    }
    if DISTINCT_PID_PROOF_CHECKS.contains(&check_name) {
        return validate_distinct_pid_proof(evidence);
    }
    if SIGKILL_PROOF_CHECKS.contains(&check_name) {
        return validate_sigkill_proof(evidence);
    }
    if EPOCH_INCREASE_PROOF_CHECKS.contains(&check_name) {
        return validate_epoch_increase_proof(evidence);
    }
    if MINIO_ENDPOINTS_PROOF_CHECKS.contains(&check_name) {
        return validate_minio_endpoints_proof(evidence);
    }
    if SCHEMA_ONLY_PROOF_CHECKS.contains(&check_name) {
        return validate_pass_with_schema_only(evidence);
    }
    vec![format!("unsupported-check:{check_name}")]
}

pub fn validate_restore_proof(evidence: &Value) -> Vec<String> {
    let Some(evidence) = evidence.as_object() else {
        return vec!["not-a-dict".to_string()];
    };
    let mut errors = require_fields(evidence, RESTORE_FIELDS);
    for field in [
        "preBackupWorkspaceDigest",
        "corruptedWorkspaceDigest",
        "postRestoreWorkspaceDigest",
    ] {
        if !missing(evidence.get(field)) && !is_plain_sha256(evidence.get(field)) {
            errors.push(format!("invalid-sha256:{field}"));
        }
    }
    let pre = value_or_empty_text(evidence.get("preBackupWorkspaceDigest"));
    let corrupted = value_or_empty_text(evidence.get("corruptedWorkspaceDigest"));
    let post = value_or_empty_text(evidence.get("postRestoreWorkspaceDigest"));
    if !pre.is_empty() && !post.is_empty() && pre != post {
        errors.push("restore-digest-mismatch".to_string());
    }
    if !pre.is_empty() && !corrupted.is_empty() && pre == corrupted {
        errors.push("workspace-was-not-corrupted".to_string());
    }
    let phase_value = [evidence.get("restorePhase"), evidence.get("phase")]
        .into_iter()
        .flatten()
        .find(|value| python_truthy(value));
    let phase = casefold(&value_or_empty_text(phase_value));
    if !phase.is_empty()
        && !matches!(
            phase.as_str(),
            "complete" | "backend-committed" | "committed"
        )
    {
        errors.push(format!("restore-phase-incomplete:{phase}"));
    }
    errors
}

pub fn validate_backup_commit_proof(evidence: &Value) -> Vec<String> {
    let Some(evidence) = evidence.as_object() else {
        return vec!["not-a-dict".to_string()];
    };
    let mut errors = require_fields(evidence, BACKUP_COMMIT_FIELDS);
    for field in ["receiptDigest", "objectSetDigest"] {
        if !missing(evidence.get(field)) && !is_plain_sha256(evidence.get(field)) {
            errors.push(format!("invalid-sha256:{field}"));
        }
    }
    let computed = evidence.get("computedReceiptSha256");
    let declared = evidence.get("receiptDigest");
    if computed.is_some_and(python_truthy)
        && declared.is_some_and(python_truthy)
        && python_text(computed) != python_text(declared)
    {
        errors.push("receipt-digest-binding-mismatch".to_string());
    }
    errors
}

pub fn validate_distinct_pid_proof(evidence: &Value) -> Vec<String> {
    let Some(evidence) = evidence.as_object() else {
        return vec!["not-a-dict".to_string()];
    };
    let mut errors = require_fields(evidence, &["pidA", "pidB"]);
    let (Some(pid_a), Some(pid_b)) = (
        python_integer(evidence.get("pidA")),
        python_integer(evidence.get("pidB")),
    ) else {
        errors.push("invalid-pid-types".to_string());
        return errors;
    };
    if !pid_a.is_positive() || !pid_b.is_positive() {
        errors.push("non-positive-pid".to_string());
    }
    if pid_a == pid_b {
        errors.push("pids-not-distinct".to_string());
    }
    errors
}

pub fn validate_sigkill_proof(evidence: &Value) -> Vec<String> {
    let Some(evidence) = evidence.as_object() else {
        return vec!["not-a-dict".to_string()];
    };
    let mut errors = require_fields(evidence, &["returncode"]);
    let Some(returncode) = python_integer(evidence.get("returncode")) else {
        errors.push("invalid-returncode".to_string());
        return errors;
    };
    if returncode.is_zero() {
        errors.push("process-a-exited-cleanly-not-killed".to_string());
    }
    errors
}

pub fn validate_epoch_increase_proof(evidence: &Value) -> Vec<String> {
    let Some(evidence) = evidence.as_object() else {
        return vec!["not-a-dict".to_string()];
    };
    let mut errors = require_fields(evidence, &["epochA", "epochB"]);
    match (
        python_integer(evidence.get("epochA")),
        python_integer(evidence.get("epochB")),
    ) {
        (Some(epoch_a), Some(epoch_b)) if epoch_b.compare(&epoch_a) != Ordering::Greater => {
            errors.push("boot-epoch-not-increased".to_string());
        }
        (Some(_), Some(_)) => {}
        _ => errors.push("invalid-epoch-types".to_string()),
    }
    errors
}

pub fn validate_minio_endpoints_proof(evidence: &Value) -> Vec<String> {
    let Some(evidence) = evidence.as_object() else {
        return vec!["not-a-dict".to_string()];
    };
    let Some(endpoints) = evidence.get("endpoints").and_then(Value::as_array) else {
        return vec!["need-three-endpoints".to_string()];
    };
    if endpoints.len() < 3 {
        return vec!["need-three-endpoints".to_string()];
    }
    let unique = endpoints
        .iter()
        .map(|endpoint| {
            python_text(Some(endpoint))
                .trim_end_matches('/')
                .to_string()
        })
        .collect::<HashSet<_>>();
    if unique.len() < 3 {
        return vec!["endpoints-not-distinct".to_string()];
    }
    Vec::new()
}

pub fn validate_pass_with_schema_only(evidence: &Value) -> Vec<String> {
    let Some(evidence) = evidence.as_object() else {
        return vec!["not-a-dict".to_string()];
    };
    // Python 4.8.0 only rejects the empty-object boundary here. Preserve that
    // compatibility quirk until the legacy check is retired under a separately versioned contract.
    if evidence.is_empty() {
        return vec!["empty-evidence".to_string()];
    }
    Vec::new()
}

fn python_integer(value: Option<&Value>) -> Option<PythonInteger> {
    PythonInteger::parse(&python_text(value))
}
