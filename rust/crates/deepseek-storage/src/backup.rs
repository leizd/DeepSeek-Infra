//! Production-grade backup engine producing cryptographic ObjectSet, ReceiptV4, and CommitV4 documents.

use sha2::{Digest, Sha256};

use crate::object_set::{ObjectInventoryEntry, ObjectSet, ObjectSetError};
use crate::receipt::{
    COMMIT_SCHEMA_VERSION, CommitError, CommitV4, DocumentError, RECEIPT_SCHEMA_VERSION,
    ReceiptError, ReceiptV4, slot_digest, validate_committed_documents,
};

#[derive(Debug)]
pub enum BackupError {
    ObjectSet(ObjectSetError),
    Receipt(ReceiptError),
    Commit(CommitError),
    Document(DocumentError),
    Serialization(serde_json::Error),
    EmptyItems,
    #[cfg(feature = "s3")]
    Storage(crate::s3::S3Error),
}

impl std::fmt::Display for BackupError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::ObjectSet(err) => write!(f, "backup object set error: {err}"),
            Self::Receipt(err) => write!(f, "backup receipt error: {err}"),
            Self::Commit(err) => write!(f, "backup commit error: {err}"),
            Self::Document(err) => write!(f, "backup document validation error: {err:?}"),
            Self::Serialization(err) => write!(f, "backup serialization error: {err}"),
            Self::EmptyItems => write!(f, "cannot create backup with zero items"),
            #[cfg(feature = "s3")]
            Self::Storage(err) => write!(f, "backup storage error: {err}"),
        }
    }
}

impl std::error::Error for BackupError {}

impl From<ObjectSetError> for BackupError {
    fn from(err: ObjectSetError) -> Self {
        Self::ObjectSet(err)
    }
}

impl From<ReceiptError> for BackupError {
    fn from(err: ReceiptError) -> Self {
        Self::Receipt(err)
    }
}

impl From<CommitError> for BackupError {
    fn from(err: CommitError) -> Self {
        Self::Commit(err)
    }
}

impl From<DocumentError> for BackupError {
    fn from(err: DocumentError) -> Self {
        Self::Document(err)
    }
}

impl From<serde_json::Error> for BackupError {
    fn from(err: serde_json::Error) -> Self {
        Self::Serialization(err)
    }
}

#[cfg(feature = "s3")]
impl From<crate::s3::S3Error> for BackupError {
    fn from(err: crate::s3::S3Error) -> Self {
        Self::Storage(err)
    }
}

#[derive(Debug, Clone)]
pub struct BackupItem {
    pub name: String,
    pub payload: Vec<u8>,
    pub is_control: bool,
}

#[derive(Debug, Clone)]
pub struct BackupParams {
    pub backup_id: String,
    pub policy_id: String,
    pub target_id: String,
    pub schedule_slot: String,
    pub run_id: String,
    pub fencing_token: u64,
    pub previous_commit_hash: String,
    pub created_at: String,
    pub snapshot_kind: String,
    pub target_generation: u64,
    pub storage_protocol: String,
}

#[derive(Debug, Clone)]
pub struct BackupResult {
    pub receipt: ReceiptV4,
    pub receipt_bytes: Vec<u8>,
    pub commit: CommitV4,
    pub commit_bytes: Vec<u8>,
    pub object_set: ObjectSet,
    pub total_bytes: u64,
}

fn hex(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        use std::fmt::Write as _;
        let _ = write!(s, "{b:02x}");
    }
    s
}

pub struct BackupEngine;

impl BackupEngine {
    /// Creates and verifies canonical ReceiptV4 and CommitV4 documents for a collection of backup items.
    pub fn build_backup(
        params: &BackupParams,
        items: &[BackupItem],
    ) -> Result<BackupResult, BackupError> {
        if items.is_empty() {
            return Err(BackupError::EmptyItems);
        }

        let mut inventory_entries = Vec::with_capacity(items.len());
        let mut control_object_digest = None;

        for item in items {
            let digest_arr: [u8; 32] = Sha256::digest(&item.payload).into();
            let digest_hex = hex(&digest_arr);
            let size = item.payload.len() as u64;

            if item.is_control || control_object_digest.is_none() {
                control_object_digest = Some(digest_hex.clone());
            }

            inventory_entries.push(ObjectInventoryEntry {
                digest: digest_hex,
                size,
            });
        }

        let control_digest = control_object_digest.expect("control object digest");
        let object_set = ObjectSet::try_new(inventory_entries)?;
        let object_set_digest = object_set.compute_digest();
        let total_size = object_set.total_size()?;

        let receipt = ReceiptV4 {
            backup_id: params.backup_id.clone(),
            chain_depth: 1,
            chunk_protocol: Some("cdc-v1".to_string()),
            control_object_digest: control_digest.clone(),
            created_at: params.created_at.clone(),
            creation_verified: true,
            lineage_id: None,
            object_set_digest: object_set_digest.clone(),
            objects: object_set.objects().to_vec(),
            parent_backup_id: None,
            pinned: false,
            policy_id: params.policy_id.clone(),
            run_id: params.run_id.clone(),
            schedule_slot: params.schedule_slot.clone(),
            schema_version: RECEIPT_SCHEMA_VERSION,
            size: total_size,
            snapshot_kind: params.snapshot_kind.clone(),
            storage_protocol: params.storage_protocol.clone(),
            target_id: params.target_id.clone(),
            base_backup_id: None,
        };

        receipt.validate()?;
        let receipt_bytes = receipt.canonical_bytes()?;
        let receipt_digest = hex(&Sha256::digest(&receipt_bytes));

        let computed_slot_digest = slot_digest(&params.schedule_slot);

        let mut commit = CommitV4 {
            backup_id: params.backup_id.clone(),
            commit_hash: String::new(),
            control_object_digest: control_digest,
            fencing_token: params.fencing_token,
            object_set_digest,
            policy_id: params.policy_id.clone(),
            previous_commit_hash: params.previous_commit_hash.clone(),
            receipt_digest,
            run_id: params.run_id.clone(),
            schedule_slot: params.schedule_slot.clone(),
            schema_version: COMMIT_SCHEMA_VERSION,
            slot_digest: computed_slot_digest,
            storage_protocol: params.storage_protocol.clone(),
            target_generation: params.target_generation,
        };

        commit.commit_hash = commit.compute_hash()?;
        let commit_bytes = commit.canonical_bytes()?;

        // Cross-verify all document invariants
        validate_committed_documents(&receipt_bytes, &commit_bytes)?;

        Ok(BackupResult {
            receipt,
            receipt_bytes,
            commit,
            commit_bytes,
            object_set,
            total_bytes: total_size,
        })
    }

    #[cfg(feature = "s3")]
    pub async fn execute_s3_backup(
        params: &BackupParams,
        items: &[BackupItem],
        transport: &crate::s3::S3Transport,
        authority: &crate::s3::StorageAuthorityProof,
        condition: crate::s3::ConditionalWrite,
    ) -> Result<BackupResult, BackupError> {
        let result = Self::build_backup(params, items)?;

        for item in items {
            let digest_arr: [u8; 32] = Sha256::digest(&item.payload).into();
            let digest_hex = hex(&digest_arr);
            transport
                .put_chunk(
                    &digest_hex,
                    bytes::Bytes::from(item.payload.clone()),
                    digest_arr,
                    authority,
                    condition.clone(),
                )
                .await?;
        }

        Ok(result)
    }
}
