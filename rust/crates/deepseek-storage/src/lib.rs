use deepseek_protocol::{
    ActionFence, AdmitError, CommandKind, admit_command, is_storage_command, validate_fence,
};

pub mod object_set;
pub mod receipt;

pub use object_set::{ChunkRef, OBJECT_SET_SCHEMA, ObjectSet, StoredObject};
pub use receipt::{CommitV4, ReceiptV4};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StorageRequest {
    pub kind: CommandKind,
    pub fence: ActionFence,
    pub object_set_digest: String,
}

pub fn plan(request: &StorageRequest, live_epoch: u64) -> Result<(), AdmitError> {
    validate_fence(&request.fence)?;
    admit_command(&request.fence, live_epoch)?;
    if !is_storage_command(request.kind) {
        return Err(AdmitError::UnknownEffect);
    }
    let _ = request.object_set_digest.as_str();
    Err(AdmitError::StorageNotAuthoritative)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn backup() -> StorageRequest {
        StorageRequest {
            kind: CommandKind::ExecuteBackup,
            fence: ActionFence {
                action_id: "act-1".to_string(),
                execution_epoch: 1,
            },
            object_set_digest: "sha256:object-set".to_string(),
        }
    }

    #[test]
    fn storage_plan_never_moves_payload_bytes() {
        assert_eq!(plan(&backup(), 0), Err(AdmitError::StorageNotAuthoritative));
        assert_eq!(
            plan(
                &StorageRequest {
                    kind: CommandKind::ExecuteRepair,
                    ..backup()
                },
                0
            ),
            Err(AdmitError::StorageNotAuthoritative)
        );
    }

    #[test]
    fn non_storage_and_stale_are_rejected_before_bytes() {
        assert_eq!(
            plan(
                &StorageRequest {
                    kind: CommandKind::SignReadiness,
                    ..backup()
                },
                0
            ),
            Err(AdmitError::UnknownEffect)
        );
        assert_eq!(plan(&backup(), 4), Err(AdmitError::StaleEpoch));
        let mut empty = backup();
        empty.fence.action_id.clear();
        assert_eq!(plan(&empty, 0), Err(AdmitError::EmptyActionId));
    }
}
