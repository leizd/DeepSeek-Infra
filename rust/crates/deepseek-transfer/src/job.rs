use serde::{Deserialize, Serialize};

pub const TRANSFER_JOB_SCHEMA: &str = "transfer-job-v1";

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct TransferCheckpoint {
    pub transfer_id: String,
    pub source_target_id: String,
    pub destination_target_id: String,
    pub transferred_bytes: u64,
    pub total_bytes: u64,
    pub transferred_chunks: usize,
    pub total_chunks: usize,
    pub completed: bool,
}

impl TransferCheckpoint {
    pub fn new(
        transfer_id: impl Into<String>,
        source_target_id: impl Into<String>,
        destination_target_id: impl Into<String>,
        total_bytes: u64,
        total_chunks: usize,
    ) -> Self {
        Self {
            transfer_id: transfer_id.into(),
            source_target_id: source_target_id.into(),
            destination_target_id: destination_target_id.into(),
            transferred_bytes: 0,
            total_bytes,
            transferred_chunks: 0,
            total_chunks,
            completed: false,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn transfer_checkpoint_serializes_camel_case() {
        let cp = TransferCheckpoint::new("tx-1", "src", "dst", 1000, 10);
        let val = serde_json::to_value(&cp).unwrap();
        let map = val.as_object().unwrap();
        assert!(map.contains_key("transferId"));
        assert!(map.contains_key("sourceTargetId"));
        assert!(map.contains_key("destinationTargetId"));
        assert!(map.contains_key("transferredBytes"));
    }
}
