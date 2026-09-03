use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct ReceiptV4 {
    pub backup_id: String,
    pub chain_depth: u32,
    pub chunk_protocol: String,
    pub control_object_digest: String,
    pub created_at: String,
    pub creation_verified: bool,
    pub lineage_id: String,
    pub object_set_digest: String,
    pub objects: Vec<String>,
    pub parent_backup_id: Option<String>,
    pub pinned: bool,
    pub policy_id: String,
    pub run_id: String,
    pub schedule_slot: String,
    pub schema_version: u32,
    pub size: u64,
    pub snapshot_kind: String,
    pub storage_protocol: String,
    pub target_id: String,
    pub base_backup_id: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct CommitV4 {
    pub backup_id: String,
    pub commit_hash: String,
    pub control_object_digest: String,
    pub fencing_token: u64,
    pub object_set_digest: String,
    pub policy_id: String,
    pub previous_commit_hash: Option<String>,
    pub receipt_digest: String,
    pub run_id: String,
    pub schedule_slot: String,
    pub schema_version: u32,
    pub slot_digest: String,
    pub storage_protocol: String,
    pub target_generation: u64,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn receipt_and_commit_fields_serialize_to_camel_case() {
        let receipt = ReceiptV4 {
            backup_id: "bk-1".to_string(),
            chain_depth: 0,
            chunk_protocol: "fastcdc-v3".to_string(),
            control_object_digest: "sha256:ctrl".to_string(),
            created_at: "2026-09-03T00:00:00Z".to_string(),
            creation_verified: true,
            lineage_id: "lin-1".to_string(),
            object_set_digest: "sha256:obj".to_string(),
            objects: vec![],
            parent_backup_id: None,
            pinned: false,
            policy_id: "pol-1".to_string(),
            run_id: "run-1".to_string(),
            schedule_slot: "slot-0".to_string(),
            schema_version: 4,
            size: 1024,
            snapshot_kind: "full".to_string(),
            storage_protocol: "s3-v1".to_string(),
            target_id: "tgt-1".to_string(),
            base_backup_id: None,
        };
        let val = serde_json::to_value(&receipt).unwrap();
        let map = val.as_object().unwrap();
        assert!(map.contains_key("backupId"));
        assert!(map.contains_key("chainDepth"));
        assert!(map.contains_key("chunkProtocol"));
        assert!(map.contains_key("controlObjectDigest"));
        assert!(map.contains_key("creationVerified"));
        assert!(map.contains_key("objectSetDigest"));
        assert_eq!(map.get("schemaVersion").unwrap().as_u64().unwrap(), 4);

        let commit = CommitV4 {
            backup_id: "bk-1".to_string(),
            commit_hash: "hash-1".to_string(),
            control_object_digest: "sha256:ctrl".to_string(),
            fencing_token: 1,
            object_set_digest: "sha256:obj".to_string(),
            policy_id: "pol-1".to_string(),
            previous_commit_hash: None,
            receipt_digest: "sha256:rcpt".to_string(),
            run_id: "run-1".to_string(),
            schedule_slot: "slot-0".to_string(),
            schema_version: 4,
            slot_digest: "sha256:slot".to_string(),
            storage_protocol: "s3-v1".to_string(),
            target_generation: 1,
        };
        let c_val = serde_json::to_value(&commit).unwrap();
        let c_map = c_val.as_object().unwrap();
        assert!(c_map.contains_key("commitHash"));
        assert!(c_map.contains_key("fencingToken"));
        assert!(c_map.contains_key("receiptDigest"));
        assert!(c_map.contains_key("targetGeneration"));
    }
}
