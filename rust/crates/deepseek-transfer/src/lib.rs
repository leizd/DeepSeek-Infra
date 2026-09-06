use deepseek_protocol::{
    ActionFence, AdmitError, CommandKind, admit_command, is_transfer_command, validate_fence,
};

pub mod authenticate;
pub mod checkpoint;
pub mod compliance;
pub mod engine;
pub mod journal;
mod pycompat;
pub mod qos;
pub mod rebalance;
pub mod repair_job;
pub mod replica_plan;
pub mod replication_job;
pub mod writer_lease;
pub use authenticate::replay_recovery_authenticate_case;
pub use checkpoint::{MultipartCheckpoint, reconcile_multipart_checkpoint};
pub use compliance::replay_replica_compliance_case;
#[cfg(feature = "s3")]
pub use engine::s3_transfer;
pub use engine::{
    DEFAULT_CHUNK_SIZE, MAX_CHUNK_SIZE, MIN_CHUNK_SIZE, TransferAuthorityProof, TransferError,
    TransferOptions, TransferReceipt, TransferSink, TransferSource, execute_transfer,
};
pub use journal::{
    FEDERATED_TRANSFER_IDENTITY_SCHEMA, FederatedTransferJournal, FederatedTransferJournalError,
    ProposedTransfer, TRANSFER_ID_DOMAIN, TRANSFER_JOURNAL_EVENT_SCHEMA,
    TRANSFER_JOURNAL_RECORD_SCHEMA, TRANSFER_STATE_PAYLOAD_SCHEMA, TransferEvent, TransferRecord,
    TransferRole, TransferState, derive_transfer_id, transfer_identity_document,
};
pub use qos::replay_transfer_qos_case;
pub use rebalance::replay_rebalance_hold_case;
pub use repair_job::replay_repair_phase_case;
pub use replica_plan::replay_replica_planner_case;
pub use replication_job::replay_replication_phase_case;
pub use writer_lease::replay_writer_lease_case;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TransferRequest {
    pub kind: CommandKind,
    pub fence: ActionFence,
    pub object_set_digest: String,
}

pub fn plan(request: &TransferRequest, live_epoch: u64) -> Result<(), AdmitError> {
    validate_fence(&request.fence)?;
    admit_command(&request.fence, live_epoch)?;
    if !is_transfer_command(request.kind) {
        return Err(AdmitError::UnknownEffect);
    }
    let _ = request.object_set_digest.as_str();
    Err(AdmitError::TransferNotAuthoritative)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn transfer() -> TransferRequest {
        TransferRequest {
            kind: CommandKind::ExecuteFederatedTransfer,
            fence: ActionFence {
                action_id: "xfer-1".to_string(),
                execution_epoch: 1,
            },
            object_set_digest: "sha256:object-set".to_string(),
        }
    }

    #[test]
    fn transfer_plan_never_moves_payload_bytes() {
        assert_eq!(
            plan(&transfer(), 1),
            Err(AdmitError::TransferNotAuthoritative)
        );
    }

    #[test]
    fn non_transfer_and_stale_are_rejected_before_bytes() {
        assert_eq!(
            plan(
                &TransferRequest {
                    kind: CommandKind::ExecuteBackup,
                    ..transfer()
                },
                1
            ),
            Err(AdmitError::UnknownEffect)
        );
        assert_eq!(plan(&transfer(), 4), Err(AdmitError::StaleEpoch));
        let mut empty = transfer();
        empty.fence.action_id.clear();
        assert_eq!(plan(&empty, 0), Err(AdmitError::EmptyActionId));
    }
}
