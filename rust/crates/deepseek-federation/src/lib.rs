use deepseek_protocol::{
    ActionFence, AdmitError, CommandKind, admit_command, is_federation_command, validate_fence,
};

pub mod attestation;
mod canonical;
mod custody;
mod identity;
pub use attestation::{
    AttestationError, CurrentSignerAuthorization, FailureDomainMetadata, MAX_REMOTE_COMMIT_BYTES,
    MAX_REMOTE_RECEIPT_BYTES, MAX_REPLICA_ATTESTATION_BYTES,
    MAX_REPLICA_ATTESTATION_LIFETIME_SECONDS, REPLICA_ATTESTATION_SCHEMA, ReplicaAttestation,
    ReplicaProofVerificationContext, ReplicaTransferBinding, ReplicaVerificationContext,
    attestation_digest, derive_transfer_id, failure_domain_from_metadata,
    verify_replica_attestation, verify_replica_attestation_document,
    verify_replica_attestation_for_proof, verify_replica_remote_documents,
};
pub use custody::{
    FederationIdentityError, OnlineFleetSigner, load_online_signer, sign_federation_document,
    unlock_private_key_envelope,
};
pub use identity::{
    PURPOSE_DR_ATTESTATION, PURPOSE_INGRESS_GRANT, PURPOSE_READINESS_ATTESTATION,
    PURPOSE_REPLICA_ATTESTATION, PURPOSE_SESSION_AUTHENTICATION, validate_fleet_identity,
    validate_online_signer_certificate, verify_federation_document,
};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SignRequest {
    pub kind: CommandKind,
    pub fence: ActionFence,
    pub payload_digest: String,
}

pub fn plan(request: &SignRequest, live_epoch: u64) -> Result<(), AdmitError> {
    validate_fence(&request.fence)?;
    admit_command(&request.fence, live_epoch)?;
    if !is_federation_command(request.kind) {
        return Err(AdmitError::UnknownEffect);
    }
    let _ = request.payload_digest.as_str();
    Err(AdmitError::FederationNotAuthoritative)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sign() -> SignRequest {
        SignRequest {
            kind: CommandKind::SignReadiness,
            fence: ActionFence {
                action_id: "sign-1".to_string(),
                execution_epoch: 1,
            },
            payload_digest: "sha256:payload".to_string(),
        }
    }

    #[test]
    fn sign_plan_never_accepts_private_keys_or_signs() {
        assert_eq!(
            plan(&sign(), 1),
            Err(AdmitError::FederationNotAuthoritative)
        );
        assert!(!format!("{:?}", sign()).contains("private"));
    }

    #[test]
    fn non_federation_and_stale_are_rejected_before_signing() {
        assert_eq!(
            plan(
                &SignRequest {
                    kind: CommandKind::ExecuteBackup,
                    ..sign()
                },
                1
            ),
            Err(AdmitError::UnknownEffect)
        );
        assert_eq!(plan(&sign(), 4), Err(AdmitError::StaleEpoch));
        let mut empty = sign();
        empty.fence.action_id.clear();
        assert_eq!(plan(&empty, 0), Err(AdmitError::EmptyActionId));
    }
}
