use deepseek_protocol::{ActionFence, AdmitError, admit_command, validate_fence};

pub mod envelope;
pub mod federated_dr;
pub mod federated_replica;
pub mod federation_trust;
pub mod predictive;
pub mod recovery_evidence;
pub mod runtime;
pub mod storage_evidence;
pub use envelope::{
    DR_READINESS_PROOF_SCHEMA, EVIDENCE_ENVELOPE_SCHEMA, EvidenceProofEnvelope, EvidenceProofError,
    MAX_EVIDENCE_PROOF_BYTES, PREDICTIVE_PLANNING_PROOF_SCHEMA, parse_evidence_proof_document,
    validate_check, validate_dr_readiness_proof, validate_evidence_proof,
    verify_evidence_proof_document,
};
pub use federated_dr::{
    FEDERATED_DR_PROOF_CHECKS, FEDERATED_DR_PROOF_SCHEMA, federated_dr_proof_digest,
    validate_federated_dr_proof,
};
pub use federated_replica::{
    FEDERATED_REPLICA_PROOF_CHECKS, FEDERATED_REPLICA_PROOF_SCHEMA, federated_replica_proof_digest,
    validate_federated_replica_check, validate_federated_replica_proof,
};
pub use federation_trust::{
    FEDERATION_TRUST_PROOF_CHECKS, FEDERATION_TRUST_PROOF_SCHEMA, federation_trust_proof_digest,
    validate_federation_trust_proof,
};
pub use predictive::{
    PREDICTIVE_PROOF_CHECKS, predictive_planning_proof_digest, validate_predictive_planning_proof,
};
pub use recovery_evidence::{
    BACKUP_COMMIT_PROOF_CHECKS, DISTINCT_PID_PROOF_CHECKS, EPOCH_INCREASE_PROOF_CHECKS,
    MINIO_ENDPOINTS_PROOF_CHECKS, RECOVERY_EVIDENCE_CHECKS, RESTORE_PROOF_CHECKS,
    SCHEMA_ONLY_PROOF_CHECKS, SIGKILL_PROOF_CHECKS, validate_backup_commit_proof,
    validate_distinct_pid_proof, validate_epoch_increase_proof, validate_minio_endpoints_proof,
    validate_pass_with_schema_only, validate_recovery_evidence_check, validate_restore_proof,
    validate_sigkill_proof,
};
pub use runtime::{
    FEDERATION_RUNTIME_PROOF_CHECKS, FEDERATION_RUNTIME_PROOF_SCHEMA,
    federation_runtime_proof_digest, validate_federation_runtime_proof,
};
pub use storage_evidence::{
    AUTONOMOUS_STORAGE_BYTES_CHECKS, validate_autonomous_storage_bytes_proof,
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
