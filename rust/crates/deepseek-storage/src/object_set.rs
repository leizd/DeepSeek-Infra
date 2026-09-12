use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use std::fmt;

pub const OBJECT_SET_SCHEMA: &str = "object-set-v1";

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ObjectInventoryEntry {
    pub digest: String,
    pub size: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ObjectSet {
    objects: Vec<ObjectInventoryEntry>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ObjectSetError {
    EmptyInventory,
    InvalidDigest { index: usize },
    DuplicateDigest(String),
    SizeOverflow,
}

impl fmt::Display for ObjectSetError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::EmptyInventory => formatter.write_str("object-set receipt inventory is empty"),
            Self::InvalidDigest { index } => {
                write!(
                    formatter,
                    "object-set receipt digest at index {index} is invalid"
                )
            }
            Self::DuplicateDigest(digest) => {
                write!(
                    formatter,
                    "object-set receipt digest is duplicated: {digest}"
                )
            }
            Self::SizeOverflow => formatter.write_str("object-set receipt size overflows u64"),
        }
    }
}

impl std::error::Error for ObjectSetError {}

impl ObjectSet {
    pub fn try_new(mut objects: Vec<ObjectInventoryEntry>) -> Result<Self, ObjectSetError> {
        validate_inventory(&objects)?;
        objects.sort_by(|left, right| {
            left.digest
                .cmp(&right.digest)
                .then(left.size.cmp(&right.size))
        });
        Ok(Self { objects })
    }

    pub fn objects(&self) -> &[ObjectInventoryEntry] {
        &self.objects
    }

    pub fn commitment(&self) -> Vec<u8> {
        let mut commitment = String::new();
        for object in &self.objects {
            commitment.push_str(&object.digest);
            commitment.push(':');
            commitment.push_str(&object.size.to_string());
            commitment.push('\n');
        }
        commitment.into_bytes()
    }

    pub fn compute_digest(&self) -> String {
        sha256_hex(&self.commitment())
    }

    pub fn total_size(&self) -> Result<u64, ObjectSetError> {
        self.objects.iter().try_fold(0_u64, |total, object| {
            total
                .checked_add(object.size)
                .ok_or(ObjectSetError::SizeOverflow)
        })
    }
}

pub fn object_inventory_digest(objects: &[ObjectInventoryEntry]) -> Result<String, ObjectSetError> {
    Ok(ObjectSet::try_new(objects.to_vec())?.compute_digest())
}

pub(crate) fn is_plain_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

pub(crate) fn sha256_hex(data: &[u8]) -> String {
    hex::encode(Sha256::digest(data))
}

fn validate_inventory(objects: &[ObjectInventoryEntry]) -> Result<(), ObjectSetError> {
    if objects.is_empty() {
        return Err(ObjectSetError::EmptyInventory);
    }
    let mut seen = HashSet::with_capacity(objects.len());
    for (index, object) in objects.iter().enumerate() {
        if !is_plain_sha256(&object.digest) {
            return Err(ObjectSetError::InvalidDigest { index });
        }
        if !seen.insert(object.digest.as_str()) {
            return Err(ObjectSetError::DuplicateDigest(object.digest.clone()));
        }
    }
    Ok(())
}

mod hex {
    use std::fmt::Write;
    pub fn encode(data: impl AsRef<[u8]>) -> String {
        let mut s = String::with_capacity(data.as_ref().len() * 2);
        for b in data.as_ref() {
            let _ = write!(s, "{:02x}", b);
        }
        s
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn object_set_uses_the_frozen_role_blind_commitment() {
        let set = ObjectSet::try_new(vec![
            ObjectInventoryEntry {
                digest: "b".repeat(64),
                size: 2,
            },
            ObjectInventoryEntry {
                digest: "a".repeat(64),
                size: 11,
            },
        ])
        .unwrap();

        assert_eq!(OBJECT_SET_SCHEMA, "object-set-v1");
        assert_eq!(
            set.commitment(),
            format!("{}:11\n{}:2\n", "a".repeat(64), "b".repeat(64)).as_bytes()
        );
        assert_eq!(
            set.compute_digest(),
            "f614451c79d5e86fe321ce7dc562fe1a7622d693315edf43fd7f6c6659528564"
        );
        assert!(!set.compute_digest().starts_with("sha256:"));
    }

    #[test]
    fn object_set_rejects_empty_invalid_and_duplicate_inventories() {
        assert_eq!(
            ObjectSet::try_new(vec![]),
            Err(ObjectSetError::EmptyInventory)
        );
        assert!(matches!(
            ObjectSet::try_new(vec![ObjectInventoryEntry {
                digest: "A".repeat(64),
                size: 1,
            }]),
            Err(ObjectSetError::InvalidDigest { index: 0 })
        ));
        let duplicate = ObjectInventoryEntry {
            digest: "c".repeat(64),
            size: 1,
        };
        assert_eq!(
            ObjectSet::try_new(vec![duplicate.clone(), duplicate]),
            Err(ObjectSetError::DuplicateDigest("c".repeat(64)))
        );
    }

    #[test]
    fn inventory_entries_reject_unknown_fields_and_non_integer_sizes() {
        assert!(
            serde_json::from_str::<ObjectInventoryEntry>(
                r#"{"digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","size":1,"role":"control"}"#
            )
            .is_err()
        );
        assert!(
            serde_json::from_str::<ObjectInventoryEntry>(
                r#"{"digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","size":true}"#
            )
            .is_err()
        );
    }
}
