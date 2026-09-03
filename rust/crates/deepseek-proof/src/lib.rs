use deepseek_protocol::{ActionFence, AdmitError, admit_command, validate_fence};

pub mod envelope;
pub use envelope::{
    DR_READINESS_PROOF_SCHEMA, EVIDENCE_ENVELOPE_SCHEMA, EvidenceProofEnvelope,
    PREDICTIVE_PLANNING_PROOF_SCHEMA,
};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProofRequest {
    pub fence: ActionFence,
    pub receipt_digest: String,
    pub commit_digest: String,
}

pub fn plan(request: &ProofRequest, live_epoch: u64) -> Result<(), AdmitError> {
    validate_fence(&request.fence)?;
    admit_command(&request.fence, live_epoch)?;
    let _ = (
        request.receipt_digest.as_str(),
        request.commit_digest.as_str(),
    );
    Err(AdmitError::ProofNotAuthoritative)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn proof() -> ProofRequest {
        ProofRequest {
            fence: ActionFence {
                action_id: "proof-1".to_string(),
                execution_epoch: 1,
            },
            receipt_digest: "sha256:receipt-v4".to_string(),
            commit_digest: "sha256:commit-v4".to_string(),
        }
    }

    #[test]
    fn proof_plan_does_not_claim_production_verification() {
        assert_eq!(plan(&proof(), 1), Err(AdmitError::ProofNotAuthoritative));
    }

    #[test]
    fn stale_and_empty_fence_are_rejected_before_verification() {
        assert_eq!(plan(&proof(), 4), Err(AdmitError::StaleEpoch));
        let mut empty = proof();
        empty.fence.action_id.clear();
        assert_eq!(plan(&empty, 0), Err(AdmitError::EmptyActionId));
        empty = proof();
        empty.fence.execution_epoch = 0;
        assert_eq!(plan(&empty, 0), Err(AdmitError::ZeroEpoch));
    }
}
