use serde::{Deserialize, Serialize};

pub const EVIDENCE_ENVELOPE_SCHEMA: &str = "evidence-proof-v2";
pub const DR_READINESS_PROOF_SCHEMA: &str = "dr-readiness-proof-v1";
pub const PREDICTIVE_PLANNING_PROOF_SCHEMA: &str = "predictive-planning-proof-v1";

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct EvidenceProofEnvelope {
    pub schema: String,
    pub proof_type: String,
    pub payload: serde_json::Value,
    pub signature: String,
    pub signer_key_id: String,
}

impl EvidenceProofEnvelope {
    pub fn new(
        proof_type: impl Into<String>,
        payload: serde_json::Value,
        signature: impl Into<String>,
        signer_key_id: impl Into<String>,
    ) -> Self {
        Self {
            schema: EVIDENCE_ENVELOPE_SCHEMA.to_string(),
            proof_type: proof_type.into(),
            payload,
            signature: signature.into(),
            signer_key_id: signer_key_id.into(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn evidence_envelope_serializes_correctly() {
        let env = EvidenceProofEnvelope::new(
            DR_READINESS_PROOF_SCHEMA,
            serde_json::json!({"status": "READY"}),
            "sig-1",
            "key-1",
        );
        let val = serde_json::to_value(&env).unwrap();
        let map = val.as_object().unwrap();
        assert_eq!(
            map.get("schema").unwrap().as_str().unwrap(),
            "evidence-proof-v2"
        );
        assert_eq!(
            map.get("proofType").unwrap().as_str().unwrap(),
            "dr-readiness-proof-v1"
        );
        assert!(map.contains_key("signerKeyId"));
    }
}
