use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::fmt;

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChunkMetadata {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub title: Option<String>,
    #[serde(skip_serializing_if = "HashMap::is_empty", default)]
    pub extra: HashMap<String, String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RagChunk {
    pub id: String,
    pub source: String,
    pub text: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub start_line: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub end_line: Option<u32>,
    pub metadata: ChunkMetadata,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ChunkValidationError {
    EmptyId,
    EmptySource,
    EmptyText,
    InvalidLineRange,
}

impl fmt::Display for ChunkValidationError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ChunkValidationError::EmptyId => write!(f, "chunk id is empty"),
            ChunkValidationError::EmptySource => write!(f, "chunk source is empty"),
            ChunkValidationError::EmptyText => write!(f, "chunk text is empty"),
            ChunkValidationError::InvalidLineRange => write!(f, "chunk line range is invalid"),
        }
    }
}

impl std::error::Error for ChunkValidationError {}

pub fn normalize_chunk(chunk: &mut RagChunk) {
    chunk.id = chunk.id.trim().to_string();
    chunk.source = chunk.source.trim().to_string();
    chunk.text = chunk.text.trim().to_string();
    if let Some(title) = chunk.metadata.title.as_mut() {
        let trimmed = title.trim();
        if trimmed.is_empty() {
            chunk.metadata.title = None;
        } else {
            *title = trimmed.to_string();
        }
    }
}

pub fn validate_chunk(chunk: &RagChunk) -> Result<(), ChunkValidationError> {
    if chunk.id.is_empty() {
        return Err(ChunkValidationError::EmptyId);
    }
    if chunk.source.is_empty() {
        return Err(ChunkValidationError::EmptySource);
    }
    if chunk.text.is_empty() {
        return Err(ChunkValidationError::EmptyText);
    }
    match (chunk.start_line, chunk.end_line) {
        (Some(start), Some(end)) if start > end => Err(ChunkValidationError::InvalidLineRange),
        _ => Ok(()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_chunk() -> RagChunk {
        RagChunk {
            id: "chunk-1".to_string(),
            source: "docs/example.md".to_string(),
            text: "Hello world".to_string(),
            start_line: Some(10),
            end_line: Some(20),
            metadata: ChunkMetadata::default(),
        }
    }

    #[test]
    fn chunk_validation_rejects_empty_id() {
        let mut chunk = sample_chunk();
        chunk.id = "   ".to_string();
        normalize_chunk(&mut chunk);
        assert!(matches!(
            validate_chunk(&chunk),
            Err(ChunkValidationError::EmptyId)
        ));
    }

    #[test]
    fn chunk_validation_rejects_invalid_line_range() {
        let mut chunk = sample_chunk();
        chunk.start_line = Some(20);
        chunk.end_line = Some(10);
        assert!(matches!(
            validate_chunk(&chunk),
            Err(ChunkValidationError::InvalidLineRange)
        ));
    }

    #[test]
    fn chunk_validation_accepts_valid_chunk() {
        let chunk = sample_chunk();
        assert!(validate_chunk(&chunk).is_ok());
    }

    #[test]
    fn chunk_normalization_trims_fields_and_empty_title() {
        let mut chunk = RagChunk {
            id: "  chunk-1  ".to_string(),
            source: "  docs/example.md  ".to_string(),
            text: "  Hello world  ".to_string(),
            start_line: None,
            end_line: None,
            metadata: ChunkMetadata {
                title: Some("   ".to_string()),
                extra: HashMap::new(),
            },
        };
        normalize_chunk(&mut chunk);
        assert_eq!(chunk.id, "chunk-1");
        assert_eq!(chunk.source, "docs/example.md");
        assert_eq!(chunk.text, "Hello world");
        assert_eq!(chunk.metadata.title, None);
    }
}
