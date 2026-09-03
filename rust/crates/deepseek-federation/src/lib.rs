use deepseek_protocol::{
    ActionFence, AdmitError, CommandKind, admit_command, is_federation_command, validate_fence,
};

pub mod attestation;
pub use attestation::{FailureDomainMetadata, ReplicaAttestation};

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
            plan(&sign(), 0),
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
                0
            ),
            Err(AdmitError::UnknownEffect)
        );
        assert_eq!(plan(&sign(), 4), Err(AdmitError::StaleEpoch));
        let mut empty = sign();
        empty.fence.action_id.clear();
        assert_eq!(plan(&empty, 0), Err(AdmitError::EmptyActionId));
    }
}
