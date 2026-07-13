/// Return the clamped dot product used by the Python semantic-cache contract.
///
/// Embeddings are normalized before they reach this boundary, so the existing
/// Python implementation intentionally avoids another norm calculation. The
/// shorter input determines the compared dimensions.
pub fn similarity(left: &[f64], right: &[f64]) -> f64 {
    if left.is_empty() || right.is_empty() {
        return 0.0;
    }
    left.iter()
        .zip(right.iter())
        .map(|(left, right)| left * right)
        .sum::<f64>()
        .clamp(0.0, 1.0)
}

/// Select the first candidate with the highest positive similarity.
///
/// Keeping the first equal-scoring candidate matches the stable SQLite row
/// order used by the Python fallback.
pub fn best_match(query: &[f64], candidates: &[Vec<f64>]) -> Option<(usize, f64)> {
    let mut best: Option<(usize, f64)> = None;
    for (index, candidate) in candidates.iter().enumerate() {
        let score = similarity(query, candidate);
        if score > best.map_or(0.0, |(_, best_score)| best_score) {
            best = Some((index, score));
        }
    }
    best
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn similarity_matches_python_clamped_dot_product() {
        assert_eq!(similarity(&[0.6, 0.8], &[0.6, 0.8]), 1.0);
        assert_eq!(similarity(&[1.0, -1.0], &[-1.0, 1.0]), 0.0);
        assert_eq!(similarity(&[2.0], &[2.0]), 1.0);
        assert_eq!(similarity(&[], &[1.0]), 0.0);
    }

    #[test]
    fn best_match_is_stable_for_ties() {
        let candidates = vec![vec![1.0, 0.0], vec![1.0, 0.0], vec![0.0, 1.0]];
        assert_eq!(best_match(&[1.0, 0.0], &candidates), Some((0, 1.0)));
    }

    #[test]
    fn best_match_ignores_zero_similarity_candidates() {
        let candidates = vec![vec![0.0, 1.0], vec![]];
        assert_eq!(best_match(&[1.0, 0.0], &candidates), None);
    }
}
