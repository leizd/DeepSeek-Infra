use std::collections::HashMap;

use deepseek_federation::{SignRequest, plan as plan_federation};
use deepseek_proof::{ProofRequest, plan as plan_proof};
use deepseek_protocol::{
    ActionFence, AdmitError, CommandKind, EffectState, admit_command, interpret_remote_outcome,
    is_federation_command, is_transfer_command, validate_authoritative_epoch_update,
};
use deepseek_storage::{StorageRequest, plan as plan_storage};
use deepseek_transfer::{TransferRequest, plan as plan_transfer};

mod authority_request;
mod service;

pub use authority_request::{
    AUTHORITY_REQUEST_SCHEMA, AuthorityRequestContext, AuthorityRequestError,
    verify_authority_request_document,
};
pub use service::WorkerRpcService;

#[derive(Debug, Default)]
pub struct Worker {
    live_epochs: HashMap<String, u64>,
    effects: HashMap<(String, u64), EffectState>,
}

impl Worker {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn install_authoritative_epoch(&mut self, fence: &ActionFence) -> Result<(), AdmitError> {
        let live = self.live_epochs.get(&fence.action_id).copied().unwrap_or(0);
        validate_authoritative_epoch_update(fence, live)?;
        self.live_epochs
            .insert(fence.action_id.clone(), fence.execution_epoch);
        Ok(())
    }

    pub fn admit(&self, fence: &ActionFence) -> Result<(), AdmitError> {
        let live = self.live_epochs.get(&fence.action_id).copied().unwrap_or(0);
        admit_command(fence, live)
    }

    pub fn query_effect(&self, fence: &ActionFence) -> Result<EffectState, AdmitError> {
        deepseek_protocol::validate_fence(fence)?;
        match self
            .effects
            .get(&(fence.action_id.clone(), fence.execution_epoch))
            .copied()
        {
            Some(state) => interpret_remote_outcome(state),
            None => Err(AdmitError::UnknownEffect),
        }
    }

    pub fn execute(&mut self, kind: CommandKind, fence: &ActionFence) -> Result<(), AdmitError> {
        self.admit(fence)?;
        if is_transfer_command(kind) {
            return plan_transfer(
                &TransferRequest {
                    kind,
                    fence: fence.clone(),
                    object_set_digest: String::new(),
                },
                fence.execution_epoch,
            );
        }
        if is_federation_command(kind) {
            return plan_federation(
                &SignRequest {
                    kind,
                    fence: fence.clone(),
                    payload_digest: String::new(),
                },
                fence.execution_epoch,
            );
        }
        plan_storage(
            &StorageRequest {
                kind,
                fence: fence.clone(),
                object_set_digest: String::new(),
            },
            fence.execution_epoch,
        )
    }

    pub fn verify_proof(
        &mut self,
        fence: &ActionFence,
        receipt_digest: String,
        commit_digest: String,
    ) -> Result<(), AdmitError> {
        self.admit(fence)?;
        plan_proof(
            &ProofRequest {
                fence: fence.clone(),
                receipt_digest,
                commit_digest,
            },
            fence.execution_epoch,
        )
    }

    pub fn record_effect(
        &mut self,
        fence: &ActionFence,
        state: EffectState,
    ) -> Result<(), AdmitError> {
        self.admit(fence)?;
        let interpreted = interpret_remote_outcome(state)?;
        self.effects.insert(
            (fence.action_id.clone(), fence.execution_epoch),
            interpreted,
        );
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fence(epoch: u64) -> ActionFence {
        ActionFence {
            action_id: "act-1".to_string(),
            execution_epoch: epoch,
        }
    }

    #[test]
    fn missing_effect_is_unknown_not_not_applied() {
        let worker = Worker::new();
        assert_eq!(
            worker.query_effect(&fence(1)),
            Err(AdmitError::UnknownEffect)
        );
    }

    #[test]
    fn invalid_fence_cannot_query_effect_state() {
        let worker = Worker::new();
        assert_eq!(
            worker.query_effect(&ActionFence {
                action_id: String::new(),
                execution_epoch: 1,
            }),
            Err(AdmitError::EmptyActionId)
        );
    }

    #[test]
    fn effect_cannot_establish_its_own_epoch() {
        let mut worker = Worker::new();
        assert_eq!(
            worker
                .record_effect(&fence(1), EffectState::Applied)
                .unwrap_err()
                .code(),
            "FENCE_MISMATCH"
        );
        assert_eq!(
            worker.query_effect(&fence(1)),
            Err(AdmitError::UnknownEffect)
        );
    }

    #[test]
    fn stale_epoch_cannot_commit() {
        let mut worker = Worker::new();
        worker.install_authoritative_epoch(&fence(4)).unwrap();
        assert_eq!(
            worker.record_effect(&fence(3), EffectState::Applied),
            Err(AdmitError::StaleEpoch)
        );
    }

    #[test]
    fn applied_effect_round_trips() {
        let mut worker = Worker::new();
        worker.install_authoritative_epoch(&fence(1)).unwrap();
        worker
            .record_effect(&fence(1), EffectState::Applied)
            .unwrap();
        assert_eq!(worker.query_effect(&fence(1)), Ok(EffectState::Applied));
    }

    #[test]
    fn storage_commands_admit_but_do_not_move_bytes() {
        let mut worker = Worker::new();
        worker.install_authoritative_epoch(&fence(1)).unwrap();
        assert_eq!(
            worker.execute(CommandKind::ExecuteBackup, &fence(1)),
            Err(AdmitError::StorageNotAuthoritative)
        );
        assert_eq!(
            worker.execute(CommandKind::ExecuteRepair, &fence(1)),
            Err(AdmitError::StorageNotAuthoritative)
        );
        assert_eq!(
            worker.query_effect(&fence(1)),
            Err(AdmitError::UnknownEffect)
        );
        worker.install_authoritative_epoch(&fence(2)).unwrap();
        assert_eq!(
            worker.execute(CommandKind::SignReadiness, &fence(2)),
            Err(AdmitError::FederationNotAuthoritative)
        );
        worker.install_authoritative_epoch(&fence(3)).unwrap();
        assert_eq!(
            worker.execute(CommandKind::ExecuteFederatedTransfer, &fence(3)),
            Err(AdmitError::TransferNotAuthoritative)
        );
    }

    #[test]
    fn proof_commands_do_not_claim_verification() {
        let mut worker = Worker::new();
        worker.install_authoritative_epoch(&fence(1)).unwrap();
        assert_eq!(
            worker.verify_proof(
                &fence(1),
                "sha256:receipt-v4".to_string(),
                "sha256:commit-v4".to_string()
            ),
            Err(AdmitError::ProofNotAuthoritative)
        );
        assert_eq!(
            worker.query_effect(&fence(1)),
            Err(AdmitError::UnknownEffect)
        );
    }

    #[test]
    fn unknown_recorded_state_is_rejected() {
        let mut worker = Worker::new();
        worker.install_authoritative_epoch(&fence(1)).unwrap();
        assert_eq!(
            worker.record_effect(&fence(1), EffectState::Unknown),
            Err(AdmitError::UnknownEffect)
        );
    }

    #[test]
    fn future_effect_epoch_cannot_advance_worker_authority() {
        let mut worker = Worker::new();
        worker.install_authoritative_epoch(&fence(2)).unwrap();
        assert_eq!(
            worker.record_effect(&fence(3), EffectState::Applied),
            Err(AdmitError::FenceMismatch)
        );
        worker
            .record_effect(&fence(2), EffectState::Applied)
            .unwrap();
        assert_eq!(worker.query_effect(&fence(2)), Ok(EffectState::Applied));
    }
}
