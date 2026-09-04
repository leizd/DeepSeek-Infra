use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use std::collections::BTreeMap;
use std::fmt;

use crate::predictive::{PREDICTIVE_PROOF_CHECKS, validate_predictive_planning_proof};
use crate::runtime::{FEDERATION_RUNTIME_PROOF_CHECKS, validate_federation_runtime_proof};
use crate::storage_evidence::{
    AUTONOMOUS_STORAGE_BYTES_CHECKS, validate_autonomous_storage_bytes_proof,
};

pub const EVIDENCE_ENVELOPE_SCHEMA: &str = "evidence-proof-v2";
pub const DR_READINESS_PROOF_SCHEMA: &str = "dr-readiness-proof-v1";
pub const PREDICTIVE_PLANNING_PROOF_SCHEMA: &str = "predictive-planning-proof-v1";
pub const MAX_EVIDENCE_PROOF_BYTES: usize = 16 * 1024 * 1024;

const DR_READINESS_CHECKS: &[&str] = &[
    "continuousDrillProducesReadinessProof",
    "drReadinessProofValid",
    "realDrReadinessProof",
    "realThreeMinioAutonomousDrillE2E",
];
const REQUIRED_DR_READINESS_FIELDS: &[&str] = &[
    "schema",
    "drillId",
    "backupId",
    "testedBackupId",
    "restoreDurationMs",
    "workspaceDigestBefore",
    "workspaceDigestAfter",
    "objectCount",
    "commitVerified",
    "receiptVerified",
    "ageVerified",
    "cleanupCompleted",
];

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct EvidenceProofEnvelope {
    pub schema: String,
    #[serde(default)]
    pub scenario: String,
    pub checks: BTreeMap<String, Value>,
    #[serde(default)]
    pub meta: Map<String, Value>,
    #[serde(flatten)]
    pub extensions: BTreeMap<String, Value>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EvidenceProofError {
    TooLarge,
    InvalidJson,
    MustBeObject,
    SchemaMismatch,
    ChecksRequired,
    InvalidDocument,
    ScenarioMismatch,
    SemanticInvalid(BTreeMap<String, Vec<String>>),
}

impl EvidenceProofError {
    pub const fn code(&self) -> &'static str {
        match self {
            Self::TooLarge => "EVIDENCE_PROOF_TOO_LARGE",
            Self::InvalidJson => "EVIDENCE_PROOF_INVALID_JSON",
            Self::MustBeObject => "EVIDENCE_PROOF_MUST_BE_OBJECT",
            Self::SchemaMismatch => "EVIDENCE_PROOF_SCHEMA_MISMATCH",
            Self::ChecksRequired => "EVIDENCE_PROOF_CHECKS_REQUIRED",
            Self::InvalidDocument => "EVIDENCE_PROOF_INVALID_DOCUMENT",
            Self::ScenarioMismatch => "EVIDENCE_PROOF_SCENARIO_MISMATCH",
            Self::SemanticInvalid(_) => "EVIDENCE_PROOF_SEMANTIC_INVALID",
        }
    }

    pub fn check_errors(&self) -> Option<&BTreeMap<String, Vec<String>>> {
        match self {
            Self::SemanticInvalid(errors) => Some(errors),
            _ => None,
        }
    }
}

impl fmt::Display for EvidenceProofError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.code())
    }
}

impl std::error::Error for EvidenceProofError {}

pub fn parse_evidence_proof_document(
    document: &[u8],
    expected_scenario: Option<&str>,
) -> Result<EvidenceProofEnvelope, EvidenceProofError> {
    if document.len() > MAX_EVIDENCE_PROOF_BYTES {
        return Err(EvidenceProofError::TooLarge);
    }
    let value: Value =
        serde_json::from_slice(document).map_err(|_| EvidenceProofError::InvalidJson)?;
    let object = value.as_object().ok_or(EvidenceProofError::MustBeObject)?;
    if object.get("schema").and_then(Value::as_str) != Some(EVIDENCE_ENVELOPE_SCHEMA) {
        return Err(EvidenceProofError::SchemaMismatch);
    }
    if !object.get("checks").is_some_and(Value::is_object) {
        return Err(EvidenceProofError::ChecksRequired);
    }
    let actual_scenario = object
        .get("scenario")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if expected_scenario.is_some_and(|expected| actual_scenario != expected) {
        return Err(EvidenceProofError::ScenarioMismatch);
    }
    serde_json::from_slice(document).map_err(|_| EvidenceProofError::InvalidDocument)
}

pub fn verify_evidence_proof_document(
    document: &[u8],
    expected_scenario: Option<&str>,
) -> Result<EvidenceProofEnvelope, EvidenceProofError> {
    let envelope = parse_evidence_proof_document(document, expected_scenario)?;
    validate_evidence_proof(&envelope)?;
    Ok(envelope)
}

pub fn validate_evidence_proof(envelope: &EvidenceProofEnvelope) -> Result<(), EvidenceProofError> {
    if envelope.schema != EVIDENCE_ENVELOPE_SCHEMA {
        return Err(EvidenceProofError::SchemaMismatch);
    }
    let mut errors = BTreeMap::new();
    if envelope.checks.is_empty() {
        errors.insert(
            "$proof".to_string(),
            vec!["proof-has-no-checks".to_string()],
        );
    }
    for (check_name, item) in &envelope.checks {
        let check_errors = validate_check(check_name, item);
        if !check_errors.is_empty() {
            errors.insert(check_name.clone(), check_errors);
        }
    }
    if errors.is_empty() {
        Ok(())
    } else {
        Err(EvidenceProofError::SemanticInvalid(errors))
    }
}

pub fn validate_check(check_name: &str, item: &Value) -> Vec<String> {
    let Some(item) = item.as_object() else {
        return vec!["check-item-must-be-object".to_string()];
    };
    let status = status_text(item.get("status"));
    if status != "PASS" {
        return vec![format!(
            "status-not-pass:{}",
            if status.is_empty() {
                "missing"
            } else {
                &status
            }
        )];
    }
    let Some(evidence) = item.get("evidence").and_then(Value::as_object) else {
        return vec!["evidence-must-be-object".to_string()];
    };
    if DR_READINESS_CHECKS.contains(&check_name) {
        return validate_dr_readiness_proof(evidence);
    }
    if FEDERATION_RUNTIME_PROOF_CHECKS.contains(&check_name) {
        return validate_federation_runtime_proof(&Value::Object(evidence.clone()));
    }
    if PREDICTIVE_PROOF_CHECKS.contains(&check_name) {
        return validate_predictive_planning_proof(&Value::Object(evidence.clone()));
    }
    if AUTONOMOUS_STORAGE_BYTES_CHECKS.contains(&check_name) {
        return validate_autonomous_storage_bytes_proof(&Value::Object(evidence.clone()));
    }
    vec![format!("unsupported-check:{check_name}")]
}

pub fn validate_dr_readiness_proof(evidence: &Map<String, Value>) -> Vec<String> {
    let mut errors = Vec::new();
    for field in REQUIRED_DR_READINESS_FIELDS {
        if missing(evidence.get(*field)) {
            errors.push(format!("missing-field:{field}"));
        }
    }

    let schema = value_text(evidence.get("schema"));
    if !schema.is_empty() && schema != DR_READINESS_PROOF_SCHEMA {
        errors.push(format!("invalid-dr-readiness-proof-schema:{schema}"));
    }
    let backup_id = value_text(evidence.get("backupId"));
    let tested_backup_id = value_text(evidence.get("testedBackupId"));
    if !backup_id.is_empty() && !tested_backup_id.is_empty() && backup_id != tested_backup_id {
        errors.push("drill-backupId-mismatch".to_string());
    }
    for field in ["workspaceDigestBefore", "workspaceDigestAfter"] {
        if !missing(evidence.get(field)) && !is_plain_sha256(evidence.get(field)) {
            errors.push(format!("invalid-sha256:{field}"));
        }
    }
    let before = value_text(evidence.get("workspaceDigestBefore"));
    let after = value_text(evidence.get("workspaceDigestAfter"));
    if !before.is_empty() && !after.is_empty() && before != after {
        errors.push("drill-workspace-digest-mismatch".to_string());
    }
    for field in [
        "commitVerified",
        "receiptVerified",
        "ageVerified",
        "cleanupCompleted",
    ] {
        if evidence.get(field) != Some(&Value::Bool(true)) {
            errors.push(format!("{field}-not-true"));
        }
    }
    if evidence
        .get("restoreDurationMs")
        .filter(|value| !value.is_null())
        .is_some_and(|value| !non_negative_number(value))
    {
        errors.push("invalid-restore-duration".to_string());
    }
    if evidence
        .get("objectCount")
        .filter(|value| !value.is_null())
        .is_some_and(|value| !non_negative_integer(value))
    {
        errors.push("invalid-object-count".to_string());
    }
    errors
}

fn status_text(value: Option<&Value>) -> String {
    match value {
        Some(Value::String(status)) => status.to_uppercase(),
        Some(Value::Bool(true)) => "TRUE".to_string(),
        Some(Value::Number(number)) if number.as_f64().is_some_and(|number| number != 0.0) => {
            number.to_string().to_uppercase()
        }
        _ => String::new(),
    }
}

fn missing(value: Option<&Value>) -> bool {
    match value {
        None | Some(Value::Null) => true,
        Some(Value::String(value)) => value.is_empty(),
        _ => false,
    }
}

fn value_text(value: Option<&Value>) -> String {
    match value {
        Some(Value::String(value)) => value.clone(),
        Some(Value::Bool(value)) => {
            if *value {
                "True".to_string()
            } else {
                String::new()
            }
        }
        Some(Value::Number(value)) => value.to_string(),
        _ => String::new(),
    }
}

fn is_plain_sha256(value: Option<&Value>) -> bool {
    value.and_then(Value::as_str).is_some_and(|value| {
        value.len() == 64
            && value
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    })
}

fn non_negative_number(value: &Value) -> bool {
    value
        .as_f64()
        .is_some_and(|number| number.is_finite() && number >= 0.0)
}

fn non_negative_integer(value: &Value) -> bool {
    value.as_u64().is_some()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn envelope_uses_the_frozen_v2_shape() {
        let envelope: EvidenceProofEnvelope = serde_json::from_value(serde_json::json!({
            "schema": "evidence-proof-v2",
            "scenario": "scenario-1",
            "checks": {},
            "meta": {"source": "native"}
        }))
        .unwrap();
        assert_eq!(envelope.schema, EVIDENCE_ENVELOPE_SCHEMA);
        assert_eq!(envelope.scenario, "scenario-1");
        assert!(envelope.checks.is_empty());
        assert_eq!(
            envelope.meta.get("source").and_then(Value::as_str),
            Some("native")
        );
    }

    #[test]
    fn invalid_status_never_reaches_the_semantic_validator() {
        assert_eq!(
            validate_check(
                "drReadinessProofValid",
                &serde_json::json!({"status": "FAIL", "evidence": {}}),
            ),
            vec!["status-not-pass:FAIL"]
        );
    }
}
