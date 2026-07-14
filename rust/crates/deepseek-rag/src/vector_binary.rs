//! Compact, bounded wire contract for vector ranking.
//!
//! The decoder borrows the validated request body and never materializes a
//! candidate matrix. Length and checked-arithmetic validation complete before
//! any scalar is scanned.

pub const REQUEST_MAGIC: &[u8; 8] = b"DSVRNK01";
pub const RESPONSE_MAGIC: &[u8; 8] = b"DSVRSP01";
pub const CONTENT_TYPE: &str = "application/vnd.deepseek.vector-rank.v1+octet-stream";
pub const HEADER_BYTES: usize = 16;
pub const RESPONSE_BYTES: usize = 24;
pub const MAX_DIMENSIONS: u32 = 4_096;
pub const MAX_CANDIDATES: u32 = 50_000;
pub const MAX_SCALARS: u64 = 1_600_000;
pub const MAX_REQUEST_BYTES: usize = HEADER_BYTES + (MAX_SCALARS as usize * 8);
pub const NO_MATCH_INDEX: u32 = u32::MAX;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BinaryError {
    InvalidBinaryMagic,
    InvalidBinaryHeader,
    InvalidDimensions,
    InvalidCandidateCount,
    PayloadLengthMismatch,
    PayloadTooLarge,
    NonFiniteVector,
    ArithmeticOverflow,
    RankingFailed,
}

impl BinaryError {
    pub const fn code(self) -> &'static str {
        match self {
            Self::InvalidBinaryMagic => "invalid_binary_magic",
            Self::InvalidBinaryHeader => "invalid_binary_header",
            Self::InvalidDimensions => "invalid_dimensions",
            Self::InvalidCandidateCount => "invalid_candidate_count",
            Self::PayloadLengthMismatch => "payload_length_mismatch",
            Self::PayloadTooLarge => "payload_too_large",
            Self::NonFiniteVector => "non_finite_vector",
            Self::ArithmeticOverflow => "arithmetic_overflow",
            Self::RankingFailed => "ranking_failed",
        }
    }

    pub const fn message(self) -> &'static str {
        match self {
            Self::InvalidBinaryMagic => "binary vector request magic is invalid",
            Self::InvalidBinaryHeader => "binary vector request header is invalid",
            Self::InvalidDimensions => "vector dimensions are outside the supported range",
            Self::InvalidCandidateCount => "candidate count is outside the supported range",
            Self::PayloadLengthMismatch => "binary vector request length does not match its header",
            Self::PayloadTooLarge => "binary vector request exceeds the scalar budget",
            Self::NonFiniteVector => "vector values must be finite",
            Self::ArithmeticOverflow => "binary vector request size arithmetic overflowed",
            Self::RankingFailed => "vector ranking could not produce a valid response",
        }
    }
}

#[derive(Debug, Clone, Copy)]
pub struct DecodedRequest<'a> {
    pub dimensions: u32,
    pub candidate_count: u32,
    scalars: &'a [u8],
}

fn read_u32(input: &[u8]) -> Result<u32, BinaryError> {
    let bytes: [u8; 4] = input
        .try_into()
        .map_err(|_| BinaryError::InvalidBinaryHeader)?;
    Ok(u32::from_le_bytes(bytes))
}

fn read_f64(input: &[u8]) -> Result<f64, BinaryError> {
    let bytes: [u8; 8] = input
        .try_into()
        .map_err(|_| BinaryError::PayloadLengthMismatch)?;
    Ok(f64::from_le_bytes(bytes))
}

pub fn expected_request_bytes(
    dimensions: u32,
    candidate_count: u32,
) -> Result<(u64, usize), BinaryError> {
    let vector_count = u64::from(candidate_count)
        .checked_add(1)
        .ok_or(BinaryError::ArithmeticOverflow)?;
    let scalar_count = u64::from(dimensions)
        .checked_mul(vector_count)
        .ok_or(BinaryError::ArithmeticOverflow)?;
    let payload_bytes = scalar_count
        .checked_mul(8)
        .ok_or(BinaryError::ArithmeticOverflow)?;
    let total_bytes = (HEADER_BYTES as u64)
        .checked_add(payload_bytes)
        .ok_or(BinaryError::ArithmeticOverflow)?;
    let total_bytes = usize::try_from(total_bytes).map_err(|_| BinaryError::ArithmeticOverflow)?;
    Ok((scalar_count, total_bytes))
}

pub fn decode_request(body: &[u8]) -> Result<DecodedRequest<'_>, BinaryError> {
    if body.len() > MAX_REQUEST_BYTES {
        return Err(BinaryError::PayloadTooLarge);
    }
    if body.len() < HEADER_BYTES {
        return Err(BinaryError::InvalidBinaryHeader);
    }
    if &body[..8] != REQUEST_MAGIC {
        return Err(BinaryError::InvalidBinaryMagic);
    }
    let dimensions = read_u32(&body[8..12])?;
    let candidate_count = read_u32(&body[12..16])?;
    let (scalar_count, expected_bytes) = expected_request_bytes(dimensions, candidate_count)?;
    if dimensions == 0 || dimensions > MAX_DIMENSIONS {
        return Err(BinaryError::InvalidDimensions);
    }
    if candidate_count == 0 || candidate_count > MAX_CANDIDATES {
        return Err(BinaryError::InvalidCandidateCount);
    }
    if scalar_count > MAX_SCALARS {
        return Err(BinaryError::PayloadTooLarge);
    }
    if body.len() != expected_bytes {
        return Err(BinaryError::PayloadLengthMismatch);
    }
    let scalars = &body[HEADER_BYTES..];
    for value in scalars.chunks_exact(8) {
        if !read_f64(value)?.is_finite() {
            return Err(BinaryError::NonFiniteVector);
        }
    }
    Ok(DecodedRequest {
        dimensions,
        candidate_count,
        scalars,
    })
}

impl DecodedRequest<'_> {
    fn scalar(&self, index: usize) -> Result<f64, BinaryError> {
        let start = index
            .checked_mul(8)
            .ok_or(BinaryError::ArithmeticOverflow)?;
        let end = start
            .checked_add(8)
            .ok_or(BinaryError::ArithmeticOverflow)?;
        read_f64(
            self.scalars
                .get(start..end)
                .ok_or(BinaryError::PayloadLengthMismatch)?,
        )
    }

    pub fn best_match(&self) -> Result<Option<(u32, f64)>, BinaryError> {
        let dimensions =
            usize::try_from(self.dimensions).map_err(|_| BinaryError::ArithmeticOverflow)?;
        let candidate_count =
            usize::try_from(self.candidate_count).map_err(|_| BinaryError::ArithmeticOverflow)?;
        let mut best: Option<(u32, f64)> = None;
        for candidate in 0..candidate_count {
            let candidate_start = dimensions
                .checked_mul(candidate + 1)
                .ok_or(BinaryError::ArithmeticOverflow)?;
            let mut score = 0.0;
            for dimension in 0..dimensions {
                score += self.scalar(dimension)? * self.scalar(candidate_start + dimension)?;
            }
            let score = score.clamp(0.0, 1.0);
            if !score.is_finite() {
                return Err(BinaryError::RankingFailed);
            }
            if score > best.map_or(0.0, |(_, best_score)| best_score) {
                best = Some((
                    u32::try_from(candidate).map_err(|_| BinaryError::ArithmeticOverflow)?,
                    score,
                ));
            }
        }
        Ok(best)
    }
}

pub fn encode_response(best: Option<(u32, f64)>) -> Result<[u8; RESPONSE_BYTES], BinaryError> {
    let (index, similarity) = best.unwrap_or((NO_MATCH_INDEX, 0.0));
    if !similarity.is_finite() || !(0.0..=1.0).contains(&similarity) {
        return Err(BinaryError::RankingFailed);
    }
    let mut response = [0_u8; RESPONSE_BYTES];
    response[..8].copy_from_slice(RESPONSE_MAGIC);
    response[8..12].copy_from_slice(&index.to_le_bytes());
    response[12..16].copy_from_slice(&0_u32.to_le_bytes());
    response[16..24].copy_from_slice(&similarity.to_le_bytes());
    Ok(response)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request(query: &[f64], candidates: &[&[f64]]) -> Vec<u8> {
        let mut body = Vec::new();
        body.extend_from_slice(REQUEST_MAGIC);
        body.extend_from_slice(&(query.len() as u32).to_le_bytes());
        body.extend_from_slice(&(candidates.len() as u32).to_le_bytes());
        for value in query
            .iter()
            .chain(candidates.iter().flat_map(|value| value.iter()))
        {
            body.extend_from_slice(&value.to_le_bytes());
        }
        body
    }

    #[test]
    fn decodes_valid_binary_request() {
        let body = request(&[1.0, 0.0], &[&[0.5, 0.0], &[1.0, 0.0]]);
        let decoded = decode_request(&body).unwrap();
        assert_eq!(decoded.dimensions, 2);
        assert_eq!(decoded.candidate_count, 2);
        assert_eq!(decoded.best_match().unwrap(), Some((1, 1.0)));
    }

    #[test]
    fn rejects_wrong_magic() {
        let mut body = request(&[1.0], &[&[1.0]]);
        body[0] = b'X';
        assert_eq!(
            decode_request(&body).unwrap_err(),
            BinaryError::InvalidBinaryMagic
        );
    }

    #[test]
    fn rejects_truncated_header() {
        assert_eq!(
            decode_request(b"DSVRNK01").unwrap_err(),
            BinaryError::InvalidBinaryHeader
        );
    }

    #[test]
    fn rejects_payload_length_mismatch() {
        let mut body = request(&[1.0], &[&[1.0]]);
        body.pop();
        assert_eq!(
            decode_request(&body).unwrap_err(),
            BinaryError::PayloadLengthMismatch
        );
    }

    #[test]
    fn rejects_trailing_bytes() {
        let mut body = request(&[1.0], &[&[1.0]]);
        body.push(0);
        assert_eq!(
            decode_request(&body).unwrap_err(),
            BinaryError::PayloadLengthMismatch
        );
    }

    #[test]
    fn rejects_zero_dimensions() {
        let mut dimensions = request(&[1.0], &[&[1.0]]);
        dimensions[8..12].copy_from_slice(&0_u32.to_le_bytes());
        assert_eq!(
            decode_request(&dimensions).unwrap_err(),
            BinaryError::InvalidDimensions
        );
    }

    #[test]
    fn rejects_zero_candidates() {
        let mut candidates = request(&[1.0], &[&[1.0]]);
        candidates[12..16].copy_from_slice(&0_u32.to_le_bytes());
        assert_eq!(
            decode_request(&candidates).unwrap_err(),
            BinaryError::InvalidCandidateCount
        );
    }

    #[test]
    fn rejects_non_finite_query() {
        let query = request(&[f64::NAN], &[&[1.0]]);
        assert_eq!(
            decode_request(&query).unwrap_err(),
            BinaryError::NonFiniteVector
        );
    }

    #[test]
    fn rejects_non_finite_candidate() {
        let candidate = request(&[1.0], &[&[f64::INFINITY]]);
        assert_eq!(
            decode_request(&candidate).unwrap_err(),
            BinaryError::NonFiniteVector
        );
    }

    #[test]
    fn rejects_checked_arithmetic_overflow() {
        assert_eq!(
            expected_request_bytes(u32::MAX, u32::MAX).unwrap_err(),
            BinaryError::ArithmeticOverflow
        );
    }

    #[test]
    fn preserves_first_match_tie() {
        let body = request(&[1.0, 0.0], &[&[1.0, 0.0], &[1.0, 0.0]]);
        assert_eq!(
            decode_request(&body).unwrap().best_match().unwrap(),
            Some((0, 1.0))
        );
    }

    #[test]
    fn matches_json_ranking_and_zero_vector_behavior() {
        let candidates = vec![vec![0.25, 0.0], vec![0.75, 0.0]];
        let body = request(&[1.0, 0.0], &[&candidates[0], &candidates[1]]);
        assert_eq!(
            decode_request(&body).unwrap().best_match().unwrap(),
            crate::vector::best_match(&[1.0, 0.0], &candidates)
                .map(|(index, score)| (index as u32, score))
        );
        let body = request(&[0.0], &[&[1.0]]);
        assert_eq!(decode_request(&body).unwrap().best_match().unwrap(), None);
    }

    #[test]
    fn encodes_fixed_size_response() {
        let response = encode_response(Some((7, 0.75))).unwrap();
        assert_eq!(response.len(), RESPONSE_BYTES);
        assert_eq!(&response[..8], RESPONSE_MAGIC);
        assert_eq!(u32::from_le_bytes(response[8..12].try_into().unwrap()), 7);
        assert_eq!(u32::from_le_bytes(response[12..16].try_into().unwrap()), 0);
        assert_eq!(
            f64::from_le_bytes(response[16..24].try_into().unwrap()),
            0.75
        );
        let empty = encode_response(None).unwrap();
        assert_eq!(
            u32::from_le_bytes(empty[8..12].try_into().unwrap()),
            NO_MATCH_INDEX
        );
    }

    #[test]
    fn does_not_allocate_before_length_validation() {
        let mut body = request(&[f64::NAN], &[&[1.0]]);
        body.pop();
        assert_eq!(
            decode_request(&body).unwrap_err(),
            BinaryError::PayloadLengthMismatch
        );
    }

    #[test]
    fn never_logs_vector_values() {
        let body = request(&[123_456.789], &[&[f64::NAN]]);
        let error = decode_request(&body).unwrap_err();
        assert_eq!(error.code(), "non_finite_vector");
        assert!(!error.message().contains("123456"));
        assert!(!error.message().contains("NaN"));
    }
}
