#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ActionFence {
    pub action_id: String,
    pub execution_epoch: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EffectState {
    Unspecified = 0,
    NotApplied = 1,
    Applied = 2,
    Unknown = 3,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AdmitError {
    EmptyActionId,
    ZeroEpoch,
    StaleEpoch,
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
            Self::UnknownEffect => "EFFECT_UNKNOWN",
            Self::StorageNotAuthoritative => "STORAGE_NOT_AUTHORITATIVE",
            Self::TransferNotAuthoritative => "TRANSFER_NOT_AUTHORITATIVE",
            Self::FederationNotAuthoritative => "FEDERATION_NOT_AUTHORITATIVE",
            Self::ProofNotAuthoritative => "PROOF_NOT_AUTHORITATIVE",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CommandKind {
    Unspecified = 0,
    ExecuteBackup = 1,
    ExecuteRestore = 2,
    ExecuteRepair = 3,
    ExecuteRebalance = 4,
    ExecuteFederatedTransfer = 5,
    SignReadiness = 6,
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
