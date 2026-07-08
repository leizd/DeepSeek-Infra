use crate::chunk::RagChunk;
use crate::query::Query;
use std::collections::HashSet;

const EXACT_MATCH_BONUS: f64 = 10.0;
const TOKEN_MATCH_BONUS: f64 = 1.0;
const SOURCE_MATCH_BONUS: f64 = 2.0;
const TITLE_MATCH_BONUS: f64 = 2.0;
const SHORT_CHUNK_MULTIPLIER: f64 = 1.1;
const SHORT_CHUNK_MAX_WORDS: usize = 20;

pub fn score_chunk(query: &Query, chunk: &RagChunk) -> f64 {
    if chunk.text.is_empty() {
        return 0.0;
    }
    let normalized_text = chunk.text.to_lowercase();
    let mut score = 0.0;

    if normalized_text.contains(&query.normalized) {
        score += EXACT_MATCH_BONUS;
    }

    let text_tokens: HashSet<String> = normalized_text
        .split_whitespace()
        .map(|s| s.to_string())
        .collect();
    for token in &query.tokens {
        if text_tokens.contains(&token.to_lowercase()) {
            score += TOKEN_MATCH_BONUS;
        }
    }

    let source_lower = chunk.source.to_lowercase();
    if source_lower.contains(&query.normalized) {
        score += SOURCE_MATCH_BONUS;
    }
    if let Some(title) = chunk.metadata.title.as_ref() {
        if title.to_lowercase().contains(&query.normalized) {
            score += TITLE_MATCH_BONUS;
        }
    }

    let word_count = normalized_text.split_whitespace().count();
    if word_count > 0 && word_count <= SHORT_CHUNK_MAX_WORDS {
        score *= SHORT_CHUNK_MULTIPLIER;
    }

    score
}

pub fn rank_chunks(query: &Query, chunks: &[RagChunk]) -> Vec<(String, f64)> {
    let mut scored: Vec<(String, f64)> = chunks
        .iter()
        .map(|chunk| (chunk.id.clone(), score_chunk(query, chunk)))
        .collect();
    scored.sort_by(|a, b| {
        b.1.partial_cmp(&a.1)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.0.cmp(&b.0))
    });
    scored
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::chunk::{ChunkMetadata, RagChunk};
    use crate::query::parse_query;

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
    fn score_exact_match_ranks_higher() {
        let query = parse_query("deepseek infra").unwrap();
        let exact = chunk("exact", "deepseek infra is a project");
        let partial = chunk("partial", "deepseek is a company");
        let exact_score = score_chunk(&query, &exact);
        let partial_score = score_chunk(&query, &partial);
        assert!(exact_score > partial_score);
    }

    #[test]
    fn score_token_overlap_ranks_relevant_chunk() {
        let query = parse_query("rust hot path").unwrap();
        let relevant = chunk("relevant", "the rust hot path is important");
        let irrelevant = chunk("irrelevant", "python runtime configuration");
        assert!(score_chunk(&query, &relevant) > score_chunk(&query, &irrelevant));
    }

    #[test]
    fn score_empty_chunk_gets_zero() {
        let query = parse_query("anything").unwrap();
        let empty = chunk("empty", "");
        assert_eq!(score_chunk(&query, &empty), 0.0);
    }

    #[test]
    fn rank_chunks_orders_by_score_descending() {
        let query = parse_query("deepseek").unwrap();
        let chunks = vec![
            chunk("b", "some unrelated text"),
            chunk("a", "deepseek is great"),
        ];
        let ranked = rank_chunks(&query, &chunks);
        assert_eq!(ranked[0].0, "a");
        assert!(ranked[0].1 > ranked[1].1);
    }
}
