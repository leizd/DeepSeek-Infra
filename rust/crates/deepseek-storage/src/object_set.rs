use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

pub const OBJECT_SET_SCHEMA: &str = "object-set-v1";

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct ChunkRef {
    pub offset: u64,
    pub length: usize,
    pub sha256: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct StoredObject {
    pub path: String,
    pub size: u64,
    pub sha256: String,
    #[serde(default)]
    pub chunks: Vec<ChunkRef>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct ObjectSet {
    pub schema_version: String,
    pub objects: Vec<StoredObject>,
}

impl ObjectSet {
    pub fn new(objects: Vec<StoredObject>) -> Self {
        Self {
            schema_version: OBJECT_SET_SCHEMA.to_string(),
            objects,
        }
    }

    pub fn compute_digest(&self) -> Result<String, serde_json::Error> {
        let bytes = serde_json::to_vec(self)?;
        let hash = Sha256::digest(&bytes);
        Ok(format!("sha256:{}", hex::encode(hash)))
    }
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
    fn object_set_schema_is_object_set_v1() {
        let set = ObjectSet::new(vec![StoredObject {
            path: "file.txt".to_string(),
            size: 100,
            sha256: "abc".to_string(),
            chunks: vec![],
        }]);
        assert_eq!(set.schema_version, "object-set-v1");
        let digest = set.compute_digest().unwrap();
        assert!(digest.starts_with("sha256:"));
    }
}
