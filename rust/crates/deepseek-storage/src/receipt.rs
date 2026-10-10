use crate::object_set::{
    OBJECT_SET_SCHEMA, ObjectInventoryEntry, ObjectSet, ObjectSetError, is_plain_sha256, sha256_hex,
};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;
use std::fmt;

pub const RECEIPT_SCHEMA_VERSION: u32 = 4;
pub const COMMIT_SCHEMA_VERSION: u32 = 4;
pub const GENESIS_COMMIT_HASH: &str =
    "0000000000000000000000000000000000000000000000000000000000000000";

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ReceiptV4 {
    pub backup_id: String,
    pub chain_depth: u32,
    pub chunk_protocol: Option<String>,
    pub control_object_digest: String,
    pub created_at: String,
    pub creation_verified: bool,
    pub lineage_id: Option<String>,
    pub object_set_digest: String,
    pub objects: Vec<ObjectInventoryEntry>,
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
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct CommitV4 {
    pub backup_id: String,
    pub commit_hash: String,
    pub control_object_digest: String,
    pub fencing_token: u64,
    pub object_set_digest: String,
    pub policy_id: String,
    pub previous_commit_hash: String,
    pub receipt_digest: String,
    pub run_id: String,
    pub schedule_slot: String,
    pub schema_version: u32,
    pub slot_digest: String,
    pub storage_protocol: String,
    pub target_generation: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReceiptError {
    InvalidSchemaVersion,
    InvalidStorageProtocol,
    InvalidCreatedAt,
    InvalidInventory(ObjectSetError),
    ObjectSetDigestMismatch,
    ControlObjectDigestInvalid,
    ControlObjectMissing,
    SizeMismatch,
}

impl fmt::Display for ReceiptError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidSchemaVersion => formatter.write_str("receipt schemaVersion must be 4"),
            Self::InvalidStorageProtocol => {
                formatter.write_str("receipt storageProtocol must be object-set-v1")
            }
            Self::InvalidCreatedAt => {
                formatter.write_str("receipt createdAt must be a canonical UTC second")
            }
            Self::InvalidInventory(error) => {
                write!(formatter, "receipt inventory is invalid: {error}")
            }
            Self::ObjectSetDigestMismatch => {
                formatter.write_str("receipt objectSetDigest does not bind its inventory")
            }
            Self::ControlObjectDigestInvalid => {
                formatter.write_str("receipt controlObjectDigest is invalid")
            }
            Self::ControlObjectMissing => {
                formatter.write_str("receipt controlObjectDigest is not in its inventory")
            }
            Self::SizeMismatch => formatter.write_str("receipt size does not equal inventory size"),
        }
    }
}

impl std::error::Error for ReceiptError {}

impl From<ObjectSetError> for ReceiptError {
    fn from(error: ObjectSetError) -> Self {
        Self::InvalidInventory(error)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CommitError {
    InvalidSchemaVersion,
    InvalidStorageProtocol,
    InvalidDigest(&'static str),
    InvalidFencingToken,
    InvalidTargetGeneration,
    SlotDigestMismatch,
    CommitHashMismatch,
    Serialization,
    ReceiptInvalid(ReceiptError),
    ReceiptEncodingMismatch,
    ReceiptDigestMismatch,
    ReceiptBindingMismatch(&'static str),
}

impl fmt::Display for CommitError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidSchemaVersion => formatter.write_str("commit schemaVersion must be 4"),
            Self::InvalidStorageProtocol => {
                formatter.write_str("commit storageProtocol must be object-set-v1")
            }
            Self::InvalidDigest(field) => {
                write!(formatter, "commit {field} is not a plain SHA-256 digest")
            }
            Self::InvalidFencingToken => {
                formatter.write_str("commit fencingToken must be at least 1")
            }
            Self::InvalidTargetGeneration => {
                formatter.write_str("commit targetGeneration must be at least 1")
            }
            Self::SlotDigestMismatch => {
                formatter.write_str("commit slotDigest does not bind scheduleSlot")
            }
            Self::CommitHashMismatch => {
                formatter.write_str("commitHash does not bind the commit body")
            }
            Self::Serialization => formatter.write_str("commit JSON serialization failed"),
            Self::ReceiptInvalid(error) => write!(formatter, "receipt is invalid: {error}"),
            Self::ReceiptEncodingMismatch => {
                formatter.write_str("receipt bytes are not canonical Receipt v4 JSON")
            }
            Self::ReceiptDigestMismatch => {
                formatter.write_str("receiptDigest does not bind the receipt bytes")
            }
            Self::ReceiptBindingMismatch(field) => {
                write!(formatter, "commit and receipt disagree on {field}")
            }
        }
    }
}

impl std::error::Error for CommitError {}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DocumentError {
    InvalidReceiptJson,
    InvalidCommitJson,
    ReceiptEncodingMismatch,
    CommitEncodingMismatch,
    ReceiptInvalid(ReceiptError),
    CommitInvalid(CommitError),
    Serialization,
}

impl fmt::Display for DocumentError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidReceiptJson => formatter.write_str("receipt is not exact Receipt v4 JSON"),
            Self::InvalidCommitJson => formatter.write_str("commit is not exact Commit v4 JSON"),
            Self::ReceiptEncodingMismatch => {
                formatter.write_str("receipt bytes are not canonical Receipt v4 JSON")
            }
            Self::CommitEncodingMismatch => {
                formatter.write_str("commit bytes are not canonical Commit v4 JSON")
            }
            Self::ReceiptInvalid(error) => write!(formatter, "receipt is invalid: {error}"),
            Self::CommitInvalid(error) => write!(formatter, "commit is invalid: {error}"),
            Self::Serialization => formatter.write_str("canonical JSON serialization failed"),
        }
    }
}

impl std::error::Error for DocumentError {}

impl ReceiptV4 {
    pub fn validate(&self) -> Result<(), ReceiptError> {
        if self.schema_version != RECEIPT_SCHEMA_VERSION {
            return Err(ReceiptError::InvalidSchemaVersion);
        }
        if self.storage_protocol != OBJECT_SET_SCHEMA {
            return Err(ReceiptError::InvalidStorageProtocol);
        }
        if !canonical_utc_timestamp_valid(&self.created_at) {
            return Err(ReceiptError::InvalidCreatedAt);
        }
        let object_set = ObjectSet::try_new(self.objects.clone())?;
        if !is_plain_sha256(&self.object_set_digest)
            || object_set.compute_digest() != self.object_set_digest
        {
            return Err(ReceiptError::ObjectSetDigestMismatch);
        }
        if !is_plain_sha256(&self.control_object_digest) {
            return Err(ReceiptError::ControlObjectDigestInvalid);
        }
        if !self
            .objects
            .iter()
            .any(|object| object.digest == self.control_object_digest)
        {
            return Err(ReceiptError::ControlObjectMissing);
        }
        if object_set.total_size()? != self.size {
            return Err(ReceiptError::SizeMismatch);
        }
        Ok(())
    }

    pub fn canonical_bytes(&self) -> Result<Vec<u8>, serde_json::Error> {
        canonical_document_bytes(self)
    }

    pub fn digest(&self) -> Result<String, serde_json::Error> {
        Ok(sha256_hex(&self.canonical_bytes()?))
    }
}

impl CommitV4 {
    pub fn compute_hash(&self) -> Result<String, serde_json::Error> {
        let mut body = BTreeMap::<&str, Value>::new();
        body.insert("backupId", Value::String(self.backup_id.clone()));
        body.insert(
            "controlObjectDigest",
            Value::String(self.control_object_digest.clone()),
        );
        body.insert("fencingToken", Value::from(self.fencing_token));
        body.insert(
            "objectSetDigest",
            Value::String(self.object_set_digest.clone()),
        );
        body.insert("policyId", Value::String(self.policy_id.clone()));
        body.insert(
            "previousCommitHash",
            Value::String(self.previous_commit_hash.clone()),
        );
        body.insert("receiptDigest", Value::String(self.receipt_digest.clone()));
        body.insert("runId", Value::String(self.run_id.clone()));
        body.insert("scheduleSlot", Value::String(self.schedule_slot.clone()));
        body.insert("schemaVersion", Value::from(self.schema_version));
        body.insert("slotDigest", Value::String(self.slot_digest.clone()));
        body.insert(
            "storageProtocol",
            Value::String(self.storage_protocol.clone()),
        );
        body.insert("targetGeneration", Value::from(self.target_generation));
        Ok(sha256_hex(&serde_json::to_vec(&body)?))
    }

    pub fn canonical_bytes(&self) -> Result<Vec<u8>, serde_json::Error> {
        canonical_document_bytes(self)
    }

    pub fn validate(&self) -> Result<(), CommitError> {
        if self.schema_version != COMMIT_SCHEMA_VERSION {
            return Err(CommitError::InvalidSchemaVersion);
        }
        if self.storage_protocol != OBJECT_SET_SCHEMA {
            return Err(CommitError::InvalidStorageProtocol);
        }
        for (field, digest) in [
            ("objectSetDigest", self.object_set_digest.as_str()),
            ("controlObjectDigest", self.control_object_digest.as_str()),
            ("receiptDigest", self.receipt_digest.as_str()),
            ("previousCommitHash", self.previous_commit_hash.as_str()),
            ("commitHash", self.commit_hash.as_str()),
        ] {
            if !is_plain_sha256(digest) {
                return Err(CommitError::InvalidDigest(field));
            }
        }
        if self.fencing_token == 0 {
            return Err(CommitError::InvalidFencingToken);
        }
        if self.target_generation == 0 {
            return Err(CommitError::InvalidTargetGeneration);
        }
        if !is_plain_sha256(&self.slot_digest)
            || self.slot_digest != slot_digest(&self.schedule_slot)
        {
            return Err(CommitError::SlotDigestMismatch);
        }
        let expected = self
            .compute_hash()
            .map_err(|_| CommitError::Serialization)?;
        if self.commit_hash != expected {
            return Err(CommitError::CommitHashMismatch);
        }
        Ok(())
    }

    pub fn validate_against_receipt(
        &self,
        receipt: &ReceiptV4,
        receipt_bytes: &[u8],
    ) -> Result<(), CommitError> {
        self.validate()?;
        receipt.validate().map_err(CommitError::ReceiptInvalid)?;
        let canonical = receipt
            .canonical_bytes()
            .map_err(|_| CommitError::Serialization)?;
        if receipt_bytes != canonical {
            return Err(CommitError::ReceiptEncodingMismatch);
        }
        if self.receipt_digest != sha256_hex(receipt_bytes) {
            return Err(CommitError::ReceiptDigestMismatch);
        }
        for (field, matches) in [
            ("backupId", self.backup_id == receipt.backup_id),
            ("policyId", self.policy_id == receipt.policy_id),
            ("runId", self.run_id == receipt.run_id),
            ("scheduleSlot", self.schedule_slot == receipt.schedule_slot),
            (
                "storageProtocol",
                self.storage_protocol == receipt.storage_protocol,
            ),
            (
                "objectSetDigest",
                self.object_set_digest == receipt.object_set_digest,
            ),
            (
                "controlObjectDigest",
                self.control_object_digest == receipt.control_object_digest,
            ),
        ] {
            if !matches {
                return Err(CommitError::ReceiptBindingMismatch(field));
            }
        }
        Ok(())
    }
}

pub fn slot_digest(schedule_slot: &str) -> String {
    sha256_hex(schedule_slot.as_bytes())
}

pub fn validate_committed_documents(
    receipt_bytes: &[u8],
    commit_bytes: &[u8],
) -> Result<(ReceiptV4, CommitV4), DocumentError> {
    let receipt: ReceiptV4 =
        serde_json::from_slice(receipt_bytes).map_err(|_| DocumentError::InvalidReceiptJson)?;
    receipt.validate().map_err(DocumentError::ReceiptInvalid)?;
    let canonical_receipt = receipt
        .canonical_bytes()
        .map_err(|_| DocumentError::Serialization)?;
    if receipt_bytes != canonical_receipt {
        return Err(DocumentError::ReceiptEncodingMismatch);
    }

    let commit: CommitV4 =
        serde_json::from_slice(commit_bytes).map_err(|_| DocumentError::InvalidCommitJson)?;
    let canonical_commit = commit
        .canonical_bytes()
        .map_err(|_| DocumentError::Serialization)?;
    if commit_bytes != canonical_commit {
        return Err(DocumentError::CommitEncodingMismatch);
    }
    commit
        .validate_against_receipt(&receipt, receipt_bytes)
        .map_err(DocumentError::CommitInvalid)?;
    Ok((receipt, commit))
}

fn canonical_utc_timestamp_valid(value: &str) -> bool {
    let bytes = value.as_bytes();
    if bytes.len() != 20
        || bytes[4] != b'-'
        || bytes[7] != b'-'
        || bytes[10] != b'T'
        || bytes[13] != b':'
        || bytes[16] != b':'
        || bytes[19] != b'Z'
    {
        return false;
    }
    let Some(year) = decimal(bytes, 0, 4) else {
        return false;
    };
    let Some(month) = decimal(bytes, 5, 2) else {
        return false;
    };
    let Some(day) = decimal(bytes, 8, 2) else {
        return false;
    };
    let Some(hour) = decimal(bytes, 11, 2) else {
        return false;
    };
    let Some(minute) = decimal(bytes, 14, 2) else {
        return false;
    };
    let Some(second) = decimal(bytes, 17, 2) else {
        return false;
    };
    if year == 0 || !(1..=12).contains(&month) || hour > 23 || minute > 59 || second > 59 {
        return false;
    }
    let leap_year = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
    let days_in_month = match month {
        2 if leap_year => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    };
    (1..=days_in_month).contains(&day)
}

fn decimal(bytes: &[u8], start: usize, length: usize) -> Option<u32> {
    bytes
        .get(start..start + length)?
        .iter()
        .try_fold(0_u32, |value, byte| {
            byte.is_ascii_digit()
                .then(|| value * 10 + u32::from(*byte - b'0'))
        })
}

fn canonical_document_bytes<T: Serialize>(document: &T) -> Result<Vec<u8>, serde_json::Error> {
    let mut value = serde_json::to_value(document)?;
    value.sort_all_objects();
    let mut bytes = serde_json::to_vec_pretty(&value)?;
    bytes.push(b'\n');
    Ok(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::object_set::ObjectInventoryEntry;

    const OBJECT_SET_DIGEST: &str =
        "f614451c79d5e86fe321ce7dc562fe1a7622d693315edf43fd7f6c6659528564";

    fn receipt() -> ReceiptV4 {
        ReceiptV4 {
            backup_id: "backup-1".to_string(),
            chain_depth: 0,
            chunk_protocol: Some("fastcdc-v3".to_string()),
            control_object_digest: "a".repeat(64),
            created_at: "2026-09-03T00:00:00Z".to_string(),
            creation_verified: true,
            lineage_id: None,
            object_set_digest: OBJECT_SET_DIGEST.to_string(),
            objects: vec![
                ObjectInventoryEntry {
                    digest: "a".repeat(64),
                    size: 11,
                },
                ObjectInventoryEntry {
                    digest: "b".repeat(64),
                    size: 2,
                },
            ],
            parent_backup_id: None,
            pinned: false,
            policy_id: "policy-1".to_string(),
            run_id: "run-1".to_string(),
            schedule_slot: "slot-1".to_string(),
            schema_version: 4,
            size: 13,
            snapshot_kind: "full".to_string(),
            storage_protocol: "object-set-v1".to_string(),
            target_id: "target-1".to_string(),
            base_backup_id: None,
        }
    }

    fn commit(receipt_digest: String) -> CommitV4 {
        let mut commit = CommitV4 {
            backup_id: "backup-1".to_string(),
            commit_hash: String::new(),
            control_object_digest: "a".repeat(64),
            fencing_token: 1,
            object_set_digest: OBJECT_SET_DIGEST.to_string(),
            policy_id: "policy-1".to_string(),
            previous_commit_hash: "0".repeat(64),
            receipt_digest,
            run_id: "run-1".to_string(),
            schedule_slot: "slot-1".to_string(),
            schema_version: 4,
            slot_digest: slot_digest("slot-1"),
            storage_protocol: "object-set-v1".to_string(),
            target_generation: 1,
        };
        commit.commit_hash = commit.compute_hash().unwrap();
        commit
    }

    #[test]
    fn receipt_and_commit_validate_exact_frozen_bindings() {
        let receipt = receipt();
        receipt.validate().unwrap();
        let receipt_bytes = receipt.canonical_bytes().unwrap();
        let receipt_digest = receipt.digest().unwrap();
        assert_eq!(
            receipt_digest,
            "2ef901c8d650ec60749709040218eccf72c65ac0a2c41e557da50fa10b7bb387"
        );
        let commit = commit(receipt_digest);
        assert_eq!(
            commit.slot_digest,
            "7a15a3648f4f2aae6c9156eed7d1577b1493d38597cf681f9787cd709c5443e2"
        );
        assert_eq!(
            commit.commit_hash,
            "b92b94140213f7da04f94fd90715092edd8f964c8e149a260693c4a7dea53087"
        );
        commit
            .validate_against_receipt(&receipt, &receipt_bytes)
            .unwrap();

        let val = serde_json::to_value(&receipt).unwrap();
        let map = val.as_object().unwrap();
        assert_eq!(map.len(), 20);
        assert_eq!(map.get("schemaVersion").unwrap().as_u64().unwrap(), 4);
        let c_val = serde_json::to_value(&commit).unwrap();
        let c_map = c_val.as_object().unwrap();
        assert_eq!(c_map.len(), 14);
    }

    #[test]
    fn receipt_rejects_tampered_inventory_and_noncanonical_documents() {
        let mut wrong_size = receipt();
        wrong_size.size += 1;
        assert_eq!(wrong_size.validate(), Err(ReceiptError::SizeMismatch));

        let mut wrong_control = receipt();
        wrong_control.control_object_digest = "c".repeat(64);
        assert_eq!(
            wrong_control.validate(),
            Err(ReceiptError::ControlObjectMissing)
        );

        for invalid in [
            "2026-09-03T00:00:00+00:00",
            "2026-09-03T00:00:00.000Z",
            "2026-02-29T00:00:00Z",
            "not-a-timestamp",
        ] {
            let mut wrong_created_at = receipt();
            wrong_created_at.created_at = invalid.to_string();
            assert_eq!(
                wrong_created_at.validate(),
                Err(ReceiptError::InvalidCreatedAt)
            );
        }

        let mut value = serde_json::to_value(receipt()).unwrap();
        value["unexpected"] = serde_json::json!(true);
        assert!(serde_json::from_value::<ReceiptV4>(value).is_err());
    }

    #[test]
    fn commit_rejects_receipt_tampering_stale_hashes_and_zero_fences() {
        let receipt = receipt();
        let canonical = receipt.canonical_bytes().unwrap();
        let mut marker = commit(receipt.digest().unwrap());

        let mut noncanonical = canonical.clone();
        noncanonical.pop();
        assert_eq!(
            marker.validate_against_receipt(&receipt, &noncanonical),
            Err(CommitError::ReceiptEncodingMismatch)
        );

        marker.fencing_token = 0;
        assert_eq!(marker.validate(), Err(CommitError::InvalidFencingToken));

        let mut marker = commit(receipt.digest().unwrap());
        marker.commit_hash = "f".repeat(64);
        assert_eq!(marker.validate(), Err(CommitError::CommitHashMismatch));

        let mut value = serde_json::to_value(commit(receipt.digest().unwrap())).unwrap();
        value["unexpected"] = serde_json::json!(true);
        assert!(serde_json::from_value::<CommitV4>(value).is_err());
    }

    #[test]
    fn committed_document_decoder_requires_canonical_exact_bytes() {
        let receipt = receipt();
        let receipt_bytes = receipt.canonical_bytes().unwrap();
        let marker = commit(receipt.digest().unwrap());
        let commit_bytes = marker.canonical_bytes().unwrap();
        let (decoded_receipt, decoded_commit) =
            validate_committed_documents(&receipt_bytes, &commit_bytes).unwrap();
        assert_eq!(decoded_receipt, receipt);
        assert_eq!(decoded_commit, marker);

        let mut noncanonical_commit = commit_bytes.clone();
        noncanonical_commit.pop();
        assert_eq!(
            validate_committed_documents(&receipt_bytes, &noncanonical_commit),
            Err(DocumentError::CommitEncodingMismatch)
        );

        assert_eq!(
            validate_committed_documents(b"not-json", &commit_bytes),
            Err(DocumentError::InvalidReceiptJson)
        );
    }
}
