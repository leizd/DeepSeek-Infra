pub mod generated {
    pub mod deepseek {
        pub mod common {
            pub mod v1 {
                tonic::include_proto!("deepseek.common.v1");
            }
        }
        pub mod action {
            pub mod v1 {
                tonic::include_proto!("deepseek.action.v1");
            }
        }
        pub mod agent {
            pub mod v1 {
                tonic::include_proto!("deepseek.agent.v1");
            }
        }
        pub mod browser {
            pub mod v1 {
                tonic::include_proto!("deepseek.browser.v1");
            }
        }
        pub mod control {
            pub mod v1 {
                tonic::include_proto!("deepseek.control.v1");
            }
        }
        pub mod evidence {
            pub mod v1 {
                tonic::include_proto!("deepseek.evidence.v1");
            }
        }
        pub mod federation {
            pub mod v1 {
                tonic::include_proto!("deepseek.federation.v1");
            }
        }
        pub mod storage {
            pub mod v1 {
                tonic::include_proto!("deepseek.storage.v1");
            }
        }
    }
}

pub use generated::deepseek::action::v1::CommandKind;
pub use generated::deepseek::common::v1::{ActionFence, EffectState};

pub const FILE_DESCRIPTOR_SET: &[u8] = tonic::include_file_descriptor_set!("deepseek.native.v1");

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AdmitError {
    EmptyActionId,
    ZeroEpoch,
    StaleEpoch,
    FenceMismatch,
    UnknownEffect,
    StorageNotAuthoritative,
    TransferNotAuthoritative,
    FederationNotAuthoritative,
    ProofNotAuthoritative,
}

impl AdmitError {
    pub fn code(self) -> &'static str {
        match self {
            Self::EmptyActionId => "EMPTY_ACTION_ID",
            Self::ZeroEpoch => "ZERO_EXECUTION_EPOCH",
            Self::StaleEpoch => "STALE_EXECUTION_EPOCH",
            Self::FenceMismatch => "FENCE_MISMATCH",
            Self::UnknownEffect => "EFFECT_UNKNOWN",
            Self::StorageNotAuthoritative => "STORAGE_NOT_AUTHORITATIVE",
            Self::TransferNotAuthoritative => "TRANSFER_NOT_AUTHORITATIVE",
            Self::FederationNotAuthoritative => "FEDERATION_NOT_AUTHORITATIVE",
            Self::ProofNotAuthoritative => "PROOF_NOT_AUTHORITATIVE",
        }
    }
}

pub fn is_storage_command(kind: CommandKind) -> bool {
    matches!(
        kind,
        CommandKind::ExecuteBackup
            | CommandKind::ExecuteRestore
            | CommandKind::ExecuteRepair
            | CommandKind::ExecuteRebalance
    )
}

pub fn is_transfer_command(kind: CommandKind) -> bool {
    matches!(kind, CommandKind::ExecuteFederatedTransfer)
}

pub fn is_federation_command(kind: CommandKind) -> bool {
    matches!(kind, CommandKind::SignReadiness)
}

pub fn validate_fence(fence: &ActionFence) -> Result<(), AdmitError> {
    if fence.action_id.is_empty() {
        return Err(AdmitError::EmptyActionId);
    }
    if fence.execution_epoch == 0 {
        return Err(AdmitError::ZeroEpoch);
    }
    Ok(())
}

pub fn admit_command(fence: &ActionFence, live_epoch: u64) -> Result<(), AdmitError> {
    validate_fence(fence)?;
    if fence.execution_epoch < live_epoch {
        return Err(AdmitError::StaleEpoch);
    }
    if live_epoch == 0 || fence.execution_epoch != live_epoch {
        return Err(AdmitError::FenceMismatch);
    }
    Ok(())
}

/// Validates an epoch supplied by the authoritative control-plane update path.
/// Effect admission must use [`admit_command`] and cannot advance live authority.
pub fn validate_authoritative_epoch_update(
    fence: &ActionFence,
    live_epoch: u64,
) -> Result<(), AdmitError> {
    validate_fence(fence)?;
    if live_epoch != 0 && fence.execution_epoch < live_epoch {
        return Err(AdmitError::StaleEpoch);
    }
    Ok(())
}

pub fn interpret_remote_outcome(state: EffectState) -> Result<EffectState, AdmitError> {
    match state {
        EffectState::Applied | EffectState::NotApplied => Ok(state),
        EffectState::Unspecified | EffectState::Unknown => Err(AdmitError::UnknownEffect),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use prost::Message;

    #[test]
    fn generated_action_fence_has_stable_wire_bytes() {
        let fence = ActionFence {
            action_id: "act-1".to_string(),
            execution_epoch: 7,
        };
        let encoded = fence.encode_to_vec();
        assert_eq!(encoded, b"\x0a\x05act-1\x10\x07");
        assert_eq!(ActionFence::decode(encoded.as_slice()).unwrap(), fence);
        assert_eq!(
            FILE_DESCRIPTOR_SET,
            include_bytes!("../../../../proto/generated/descriptor.pb")
        );
    }

    #[test]
    fn empty_action_id_is_rejected() {
        let fence = ActionFence {
            action_id: String::new(),
            execution_epoch: 1,
        };
        assert_eq!(validate_fence(&fence), Err(AdmitError::EmptyActionId));
    }

    #[test]
    fn zero_epoch_is_rejected() {
        let fence = ActionFence {
            action_id: "act-1".to_string(),
            execution_epoch: 0,
        };
        assert_eq!(validate_fence(&fence), Err(AdmitError::ZeroEpoch));
    }

    #[test]
    fn stale_epoch_is_rejected() {
        let fence = ActionFence {
            action_id: "act-1".to_string(),
            execution_epoch: 3,
        };
        assert_eq!(admit_command(&fence, 4), Err(AdmitError::StaleEpoch));
        assert_eq!(admit_command(&fence, 3), Ok(()));
    }

    #[test]
    fn command_epoch_must_match_established_authority() {
        let fence = ActionFence {
            action_id: "act-1".to_string(),
            execution_epoch: 3,
        };
        assert_eq!(admit_command(&fence, 0), Err(AdmitError::FenceMismatch));
        assert_eq!(admit_command(&fence, 2), Err(AdmitError::FenceMismatch));
    }

    #[test]
    fn authoritative_epoch_update_is_the_only_advance_path() {
        let fence = ActionFence {
            action_id: "act-1".to_string(),
            execution_epoch: 3,
        };
        assert_eq!(validate_authoritative_epoch_update(&fence, 0), Ok(()));
        assert_eq!(validate_authoritative_epoch_update(&fence, 2), Ok(()));
        assert_eq!(
            validate_authoritative_epoch_update(&fence, 4),
            Err(AdmitError::StaleEpoch)
        );
    }

    #[test]
    fn unknown_effect_cannot_be_not_applied() {
        assert_eq!(
            interpret_remote_outcome(EffectState::Unknown),
            Err(AdmitError::UnknownEffect)
        );
        assert_eq!(
            interpret_remote_outcome(EffectState::Unspecified),
            Err(AdmitError::UnknownEffect)
        );
        assert_eq!(
            interpret_remote_outcome(EffectState::NotApplied),
            Ok(EffectState::NotApplied)
        );
        assert_eq!(AdmitError::UnknownEffect.code(), "EFFECT_UNKNOWN");
        assert_eq!(
            AdmitError::StorageNotAuthoritative.code(),
            "STORAGE_NOT_AUTHORITATIVE"
        );
        assert!(is_storage_command(CommandKind::ExecuteBackup));
        assert!(!is_storage_command(CommandKind::ExecuteFederatedTransfer));
        assert!(is_transfer_command(CommandKind::ExecuteFederatedTransfer));
        assert!(!is_storage_command(CommandKind::SignReadiness));
        assert!(!is_storage_command(CommandKind::Unspecified));
        assert_eq!(
            AdmitError::TransferNotAuthoritative.code(),
            "TRANSFER_NOT_AUTHORITATIVE"
        );
        assert!(is_federation_command(CommandKind::SignReadiness));
        assert!(!is_federation_command(CommandKind::ExecuteBackup));
        assert_eq!(
            AdmitError::FederationNotAuthoritative.code(),
            "FEDERATION_NOT_AUTHORITATIVE"
        );
        assert_eq!(
            AdmitError::ProofNotAuthoritative.code(),
            "PROOF_NOT_AUTHORITATIVE"
        );
    }
}
