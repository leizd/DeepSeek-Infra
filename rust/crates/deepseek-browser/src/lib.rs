//! The browser engine sidecar (ADR-0050).
//!
//! The engine is a separate optional process: its own binary, its own image, reached
//! over versioned gRPC/Protobuf (`deepseek.browser.v1.BrowserEngine`). It spawns a
//! headless Chromium itself and speaks CDP to it — no Python, no Node driver, no
//! second language runtime in the native plane.
//!
//! # The seam does not move
//!
//! `execute_browser_action` and the safety gate stay in `deepseek-policy`, and
//! `playwright_available()` stays the single switch: while no engine answers, the
//! static controller runs and its parity is already pinned by
//! `browser_page_parity_probe`. A deployment with no Chromium is **degraded, not
//! broken** — `Status` reports `available: false` with the reason, and the gateway
//! keeps its own switch off.
//!
//! # Ownership does not move
//!
//! The sidecar returns bytes and paths. It opens no durable store: profiles and
//! staged downloads live under a per-process directory that a closed session
//! removes. Media and RAG snapshot writes stay with their current owner.
//!
//! # Fences
//!
//! Every request binds an `ActionFence` (`actionId + executionEpoch`), as ADR-0049
//! requires of every native boundary. The engine has no authority of its own and no
//! epoch state to advance, so the fence is validated and otherwise passed through as
//! the call's ordering id.
//!
//! # What is not equal to Playwright, by construction
//!
//! Listed in [`docs/specs/browser-engine-sidecar.md`](../../../../docs/specs/browser-engine-sidecar.md)
//! rather than hidden: locator auto-waiting and actionability checks, the exact
//! `domcontentloaded` timing and lazy-load side effects, the download event window
//! and `suggested_filename` semantics, and the wording of timeout errors. "Byte
//! parity with Playwright" is not a goal and must not be claimed.

pub mod engine;
pub mod sidecar;

/// The oracle's `AppError` shape (`deepseek_infra/core/errors.py`): a message plus a
/// stable code and HTTP status.
///
/// The engine is a separate process from the gateway, so it cannot borrow the
/// gateway's or the policy crate's copy of this type — and it must not grow a
/// dependency on either just to share three fields. The shape is what matters: the
/// sidecar maps these onto gRPC statuses, and the gateway maps those back onto the
/// oracle's HTTP envelope.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AppError {
    pub message: String,
    pub code: &'static str,
    pub status: u16,
}

/// The error codes this crate needs, mirroring `ErrorCode`.
pub mod codes {
    /// `ErrorCode.INVALID_PAYLOAD`
    pub const INVALID_PAYLOAD: &str = "invalid_payload";
    /// `ErrorCode.NOT_FOUND`
    pub const NOT_FOUND: &str = "not_found";
    /// `ErrorCode.INTERNAL`
    pub const INTERNAL: &str = "internal";
    /// The oracle's `PlaywrightTimeoutError` arm. The oracle surfaces a Playwright
    /// timeout as `ErrorCode.UPSTREAM_TIMEOUT` (504), and CDP has no single code for
    /// it, so a deadline the engine enforces is reported as the same code the oracle
    /// would have produced.
    pub const UPSTREAM_TIMEOUT: &str = "upstream_timeout";
}

impl AppError {
    pub fn new(code: &'static str, message: impl Into<String>) -> Self {
        let status = match code {
            codes::INVALID_PAYLOAD => 400,
            codes::NOT_FOUND => 404,
            codes::UPSTREAM_TIMEOUT => 504,
            _ => 500,
        };
        Self {
            message: message.into(),
            code,
            status,
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
    fn the_error_codes_carry_the_oracles_statuses() {
        assert_eq!(AppError::new(codes::INVALID_PAYLOAD, "bad").status, 400);
        assert_eq!(AppError::new(codes::NOT_FOUND, "nope").status, 404);
        assert_eq!(AppError::new(codes::INTERNAL, "boom").status, 500);
        assert_eq!(AppError::new(codes::UPSTREAM_TIMEOUT, "slow").status, 504);
        assert_eq!(AppError::new(codes::INTERNAL, "boom").to_string(), "boom");
    }
}
