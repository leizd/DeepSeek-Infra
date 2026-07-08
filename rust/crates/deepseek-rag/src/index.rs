use crate::chunk::{RagChunk, validate_chunk};
use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::fmt;

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct IndexMetadata {
    pub chunks: Vec<RagChunk>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum IndexValidationError {
    DuplicateChunkId(String),
    InvalidChunk(String, String),
}

impl fmt::Display for IndexValidationError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            IndexValidationError::DuplicateChunkId(id) => write!(f, "duplicate chunk id: {id}"),
            IndexValidationError::InvalidChunk(id, reason) => {
                write!(f, "invalid chunk {id}: {reason}")
            }
        }
    }
}

impl std::error::Error for IndexValidationError {}

pub fn validate_index_metadata(index: &IndexMetadata) -> Result<(), IndexValidationError> {
    let mut seen = HashSet::new();
    for chunk in &index.chunks {
        if !seen.insert(chunk.id.clone()) {
            return Err(IndexValidationError::DuplicateChunkId(chunk.id.clone()));
        }
        validate_chunk(chunk)
            .map_err(|e| IndexValidationError::InvalidChunk(chunk.id.clone(), e.to_string()))?;
    }
    Ok(())
}

pub fn index_metadata_json_roundtrip(
    index: &IndexMetadata,
) -> Result<IndexMetadata, serde_json::Error> {
    let json = serde_json::to_string(index)?;
    serde_json::from_str(&json)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::chunk::{ChunkMetadata, RagChunk};

    fn chunk(id: &str, text: &str) -> RagChunk {
        RagChunk {
            id: id.to_string(),
            source: "docs/example.md".to_string(),
            text: text.to_string(),
            start_line: None,
            end_line: None,
            metadata: ChunkMetadata::default(),
        }
    }

    #[test]
    fn index_validation_rejects_duplicate_chunk_ids() {
        let index = IndexMetadata {
            chunks: vec![chunk("a", "one"), chunk("a", "two")],
        };
        assert!(matches!(
            validate_index_metadata(&index),
            Err(IndexValidationError::DuplicateChunkId(_))
        ));
    }

    #[test]
    fn index_metadata_roundtrips_json() {
        let index = IndexMetadata {
            chunks: vec![
                chunk("a", "first chunk"),
                RagChunk {
                    id: "b".to_string(),
                    source: "docs/other.md".to_string(),
                    text: "second chunk".to_string(),
                    start_line: Some(1),
                    end_line: Some(5),
                    metadata: ChunkMetadata {
                        title: Some("Other doc".to_string()),
                        extra: [("key".to_string(), "value".to_string())]
                            .into_iter()
                            .collect(),
                    },
                },
            ],
        };
        let roundtripped = index_metadata_json_roundtrip(&index).unwrap();
        assert_eq!(roundtripped, index);
    }

    #[test]
    fn index_validation_rejects_invalid_chunk() {
        let mut bad = chunk("x", "text");
        bad.start_line = Some(10);
        bad.end_line = Some(5);
        let index = IndexMetadata { chunks: vec![bad] };
        assert!(matches!(
            validate_index_metadata(&index),
            Err(IndexValidationError::InvalidChunk(_, _))
        ));
    }
}
