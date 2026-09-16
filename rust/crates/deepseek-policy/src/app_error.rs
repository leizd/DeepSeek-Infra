//! The oracle's `AppError` shape: a message plus a stable error code and HTTP
//! status.
//!
//! `deepseek_infra/core/errors.py` defines `AppError` with `code`, `status` and
//! `details`, and the data layer raises it for every rejection a caller can
//! expect. This carries the same three fields.
//!
//! Relationship to [`crate::mutation_gate::GateError`]: that type models the three
//! *different* exception shapes one module raises (`AppError`, a bare
//! `RuntimeError`, and `OSError`), which is why it carries a `kind` discriminant
//! and optional code/status. This is only the `AppError` shape, for modules that
//! raise nothing else.

/// The error code values this crate needs, mirroring `ErrorCode`.
pub mod codes {
    /// `ErrorCode.INVALID_PAYLOAD`
    pub const INVALID_PAYLOAD: &str = "invalid_payload";
    /// `ErrorCode.NOT_FOUND`
    pub const NOT_FOUND: &str = "not_found";
    /// `ErrorCode.INVALID_REQUEST`
    pub const INVALID_REQUEST: &str = "invalid_request";
    /// `ErrorCode.SENSITIVE_CONTENT`
    pub const SENSITIVE_CONTENT: &str = "sensitive_content";
    /// `ErrorCode.FILE_INDEX_EXPIRED`
    pub const FILE_INDEX_EXPIRED: &str = "file_index_expired";
    /// `ErrorCode.INTERNAL`
    pub const INTERNAL: &str = "internal";
    /// `ErrorCode.MISSING_API_KEY`
    pub const MISSING_API_KEY: &str = "missing_api_key";
    /// `ErrorCode.UPSTREAM_TIMEOUT`
    pub const UPSTREAM_TIMEOUT: &str = "upstream_timeout";
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AppError {
    pub message: String,
    pub code: &'static str,
    pub status: u16,
}

impl AppError {
    /// `AppError(message, code=INVALID_PAYLOAD)`, whose default status is `400`.
    pub fn invalid_payload(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        }
    }

    /// `AppError(message, code=NOT_FOUND, status=404)`.
    pub fn not_found(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            code: codes::NOT_FOUND,
            status: 404,
        }
    }
}

impl std::fmt::Display for AppError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(formatter, "{}", self.message)
    }
}

impl std::error::Error for AppError {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn constructors_carry_the_oracles_codes_and_statuses() {
        let invalid = AppError::invalid_payload("bad");
        assert_eq!(invalid.code, codes::INVALID_PAYLOAD);
        assert_eq!(invalid.status, 400);
        assert_eq!(invalid.to_string(), "bad");

        let missing = AppError::not_found("nope");
        assert_eq!(missing.code, codes::NOT_FOUND);
        assert_eq!(missing.status, 404);
    }
}
