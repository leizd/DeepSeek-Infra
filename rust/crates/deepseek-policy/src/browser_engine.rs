//! The browser engine boundary, from the policy crate's side.
//!
//! `browser.rs` owns the safety gate, the session registry and the static fallback
//! controller. It does **not** own a browser: the engine is a separate Rust process
//! reached over versioned gRPC/Protobuf (`proto/browser/v1/browser.proto`,
//! ADR-0050), and this module is the seam.
//!
//! # Why a trait and not a client
//!
//! `deepseek-policy` is the policy and data plane; it has no HTTP or gRPC
//! dependency on purpose, and giving it one would put a network client inside the
//! crate that every other policy branch links. So the crate declares *what it needs*
//! — [`BrowserEngine`] — and the gateway, which already speaks gRPC, implements it.
//!
//! # What the seam does not move
//!
//! - **The safety gate runs before the engine.** [`crate::browser_safety::evaluate_action`]
//!   is reached first, so a refused URL never opens a socket. The engine does not
//!   re-implement the policy, because two policies can drift.
//! - **The session registry stays here.** The engine keys its browser contexts by
//!   the same session id, but the session's existence, project, status and recorded
//!   controller kind are this crate's state.
//! - **`playwright_available()` stays the single switch.** An engine that answers
//!   `available: false` leaves the static controller in place; that is a degraded
//!   deployment, not a broken one.
//!
//! # Fences
//!
//! Every engine call carries an [`EngineFence`] (`actionId + executionEpoch`), as
//! ADR-0049 requires of every native boundary. The engine has no durable store and
//! no authority of its own, so the fence is the call's ordering id — and a worker
//! must never advance an epoch because a caller sent a larger one, which is why this
//! type is constructed from the request and never from the engine's answer.

use serde_json::Value;

/// The controller kind the engine reports when it is driving a real browser.
pub const ENGINE_KIND_CDP: &str = "cdp_chromium";
/// The code an engine answers with when the deployment has no browser configured.
pub const ENGINE_NOT_CONFIGURED: &str = "browser_engine_not_configured";
/// The code an engine answers with when a session id has no browser attached.
pub const ENGINE_SESSION_NOT_FOUND: &str = "browser_session_not_found";

/// The fence every engine call carries.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EngineFence {
    pub action_id: String,
    pub execution_epoch: u64,
}

impl EngineFence {
    /// A fence for a tool call, from the request's own ordering id.
    ///
    /// The tool surface has no epoch of its own — the browser is not a durable
    /// authority — so the epoch is the request's, and `1` is the only honest value
    /// when the caller has none: zero is refused by every native boundary as
    /// "no epoch supplied", and inventing a larger number would be a worker
    /// promoting itself.
    pub fn for_request(request_id: &str, execution_epoch: u64) -> Self {
        Self {
            action_id: request_id.to_string(),
            execution_epoch: if execution_epoch == 0 {
                1
            } else {
                execution_epoch
            },
        }
    }
}

/// What the engine answered. One type, because the actions differ in their payload
/// and not in their admission.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct EngineOutcome {
    /// The engine's controller kind (`cdp_chromium`).
    pub engine_kind: String,
    pub url: String,
    pub title: String,
    pub text: String,
    pub html: String,
    pub selector: String,
    pub links: Vec<EngineLink>,
    pub screenshot: Option<Vec<u8>>,
    pub mime_type: String,
    pub selected: Vec<String>,
    pub download: Option<EngineDownload>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct EngineLink {
    pub href: String,
    pub text: String,
    pub title: String,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct EngineDownload {
    /// The browser's own name for the file. Under CDP's `allowAndName` this is a
    /// GUID, not the link's `download` attribute — a listed divergence.
    pub filename: String,
    pub data: Vec<u8>,
}

/// An engine failure, in the oracle's `AppError` shape so the tool envelope is the
/// same whether the answer came from the static controller or from a browser.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EngineError {
    pub message: String,
    pub code: String,
    pub status: u16,
}

impl EngineError {
    pub fn new(code: impl Into<String>, message: impl Into<String>) -> Self {
        let code = code.into();
        Self {
            status: status_for(&code),
            code,
            message: message.into(),
        }
    }
}

impl std::fmt::Display for EngineError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(formatter, "{}", self.message)
    }
}

impl std::error::Error for EngineError {}

fn status_for(code: &str) -> u16 {
    match code {
        crate::app_error::codes::INVALID_PAYLOAD => 400,
        crate::app_error::codes::NOT_FOUND | ENGINE_SESSION_NOT_FOUND => 404,
        crate::app_error::codes::UPSTREAM_TIMEOUT => 504,
        _ => 500,
    }
}

/// What a browser action asks the engine to do.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum EngineAction {
    #[default]
    Status,
    OpenUrl,
    ReadPage,
    ExtractLinks,
    Screenshot,
    Click,
    TypeText,
    Select,
    Scroll,
    Download,
    /// Tear down the browser context a session id names. The gateway's registry stays
    /// the authority on whether the session existed; this only releases the browser.
    CloseSession,
}

impl EngineAction {
    /// The engine action for a tool action name, or `None` when the action is one
    /// the engine does not serve.
    pub fn for_tool_action(action: &str) -> Option<Self> {
        match action {
            "open_url" => Some(Self::OpenUrl),
            "read_page" | "save_snapshot" => Some(Self::ReadPage),
            "extract_links" => Some(Self::ExtractLinks),
            "extract_dom" => Some(Self::ReadPage),
            "screenshot" => Some(Self::Screenshot),
            "click" => Some(Self::Click),
            "type_text" => Some(Self::TypeText),
            "select" => Some(Self::Select),
            "scroll" => Some(Self::Scroll),
            "download" => Some(Self::Download),
            "close_session" => Some(Self::CloseSession),
            _ => None,
        }
    }
}

/// One engine call.
#[derive(Debug, Clone, Default)]
pub struct EngineRequest {
    pub action: EngineAction,
    pub session_id: String,
    pub url: String,
    pub selector: String,
    pub text: String,
    pub value: String,
    pub x: i32,
    pub y: i32,
    /// Where the engine stages a download before returning its bytes. The engine
    /// removes the staging directory when the session closes.
    pub download_dir: Option<String>,
}

/// Whether an engine can be reached, and what it can do.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct EngineStatus {
    pub available: bool,
    pub engine_kind: String,
    pub chromium_revision: String,
    pub reason: String,
}

/// The engine, as the policy crate needs it.
///
/// Implementations must be safe to call from the blocking pool: the tool loop runs
/// its branches under `spawn_blocking`, so a synchronous call is the expected shape.
pub trait BrowserEngine: Send + Sync {
    fn status(&self, fence: &EngineFence) -> Result<EngineStatus, EngineError>;
    fn execute(
        &self,
        fence: &EngineFence,
        request: &EngineRequest,
    ) -> Result<EngineOutcome, EngineError>;
}

/// The `Status` answer as the `browser_status` envelope reports it.
pub fn status_envelope(status: &EngineStatus) -> Value {
    serde_json::json!({
        "available": status.available,
        "engine": ENGINE_KIND_CDP,
        "engineKind": status.engine_kind,
        "chromiumRevision": status.chromium_revision,
        "reason": status.reason,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_request_fence_never_carries_a_zero_epoch() {
        let fence = EngineFence::for_request("act-1", 0);
        assert_eq!(fence.action_id, "act-1");
        assert_eq!(fence.execution_epoch, 1);
        assert_eq!(EngineFence::for_request("act-1", 7).execution_epoch, 7);
    }

    #[test]
    fn the_engine_actions_are_the_ones_the_tool_surface_names() {
        assert_eq!(
            EngineAction::for_tool_action("open_url"),
            Some(EngineAction::OpenUrl)
        );
        assert_eq!(
            EngineAction::for_tool_action("save_snapshot"),
            Some(EngineAction::ReadPage)
        );
        assert_eq!(
            EngineAction::for_tool_action("extract_dom"),
            Some(EngineAction::ReadPage)
        );
        assert_eq!(
            EngineAction::for_tool_action("close_session"),
            Some(EngineAction::CloseSession)
        );
        assert_eq!(EngineAction::for_tool_action("nonsense"), None);
    }

    #[test]
    fn an_engine_error_carries_the_oracles_status() {
        assert_eq!(
            EngineError::new(crate::app_error::codes::INVALID_PAYLOAD, "bad").status,
            400
        );
        assert_eq!(
            EngineError::new(ENGINE_SESSION_NOT_FOUND, "gone").status,
            404
        );
        assert_eq!(
            EngineError::new(crate::app_error::codes::UPSTREAM_TIMEOUT, "slow").status,
            504
        );
        assert_eq!(EngineError::new(ENGINE_NOT_CONFIGURED, "none").status, 500);
        assert_eq!(EngineError::new("x", "boom").to_string(), "boom");
    }
}
