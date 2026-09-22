//! The gRPC sidecar: one browser context per session id, one call per action.
//!
//! ADR-0050 stage 2. Stage 1 shipped the boundary and refused every action; this
//! module is the engine behind it. What it does **not** change is the seam: the
//! gateway keeps `execute_browser_action` and the safety gate, and
//! `playwright_available()` stays the single switch. The sidecar is reached only
//! when a deployment has both an engine binary and a Chromium to drive.
//!
//! # Session ids
//!
//! The gateway owns the session-id mapping it already has; this process keys its
//! browser contexts by the same string. A call naming a session that does not exist
//! is `not_found`, not an implicit create — the oracle's `get_session` raises the
//! same way, and an implicit create would let one client drive another's browser.
//!
//! # Fences
//!
//! Every request carries an `ActionFence` (`actionId + executionEpoch`), as ADR-0049
//! requires of every native boundary. The engine has no durable store and no
//! authority of its own, so the fence is the call's ordering id: it is validated
//! (non-empty action id, non-zero epoch) and otherwise passed through. A worker must
//! not advance an epoch because a caller sent a larger one, and this process never
//! does — there is no epoch state here to advance.
//!
//! # What is deliberately absent
//!
//! - **No durable store.** Profiles and downloads live under a per-process
//!   directory that is removed when a session closes; media/RAG ownership does not
//!   move (ADR-0050 §4).
//! - **No safety policy.** The gateway's `browser_safety` gate runs *before* the
//!   sidecar is reached. Duplicating it here would create two policies that could
//!   drift; a sidecar that enforced a different one would be worse than none.
//! - **No actionability retry.** Playwright's locator auto-waits; CDP does not. The
//!   specification lists that as a non-equal-by-construction divergence.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use deepseek_protocol::generated::deepseek::browser::v1::{
    ActionResponse, CloseSessionRequest, CloseSessionResponse, DownloadRequest, DownloadResponse,
    Link as ProtoLink, LinksResponse, OpenUrlRequest, PageResponse, ReadPageRequest,
    ScreenshotRequest, ScreenshotResponse, ScrollRequest, SelectRequest, SelectResponse,
    SelectorRequest, SessionRequest, StatusRequest, StatusResponse, TypeTextRequest,
    browser_engine_server::BrowserEngine as BrowserEngineApi,
};
use deepseek_protocol::generated::deepseek::common::v1::{ActionFence, ErrorDetail};
use tokio::sync::Mutex;
use tonic::{Request, Response, Status};

use crate::engine::{Engine, EngineSettings};
use crate::{AppError, codes};

/// The kind a CDP engine reports; the gateway's `playwright_available()` is what
/// turns it into a controller choice.
pub const ENGINE_KIND_CDP: &str = "cdp_chromium";
/// The machine code carried when a deployment has no engine to drive.
pub const ENGINE_NOT_CONFIGURED: &str = "browser_engine_not_configured";
/// Where per-session profile directories are created.
const PROFILE_ROOT_ENV: &str = "DEEPSEEK_BROWSER_PROFILE_ROOT";
/// Where downloads are staged before they are returned as bytes.
const DOWNLOAD_ROOT_ENV: &str = "DEEPSEEK_BROWSER_DOWNLOAD_ROOT";

/// One live session: the browser, its profile, and where its downloads land.
struct Session {
    engine: Mutex<Engine>,
    download_dir: PathBuf,
}

/// The engine behind the boundary.
///
/// `settings` is captured at construction so a deployment that has no Chromium
/// reports `available: false` for every `Status` call rather than discovering the
/// absence per action. `sessions` is the process's own registry, keyed by the
/// gateway's session id.
pub struct BrowserEngineService {
    settings: EngineSettings,
    profile_root: PathBuf,
    download_root: PathBuf,
    sessions: Mutex<HashMap<String, Arc<Session>>>,
}

impl BrowserEngineService {
    /// From the server environment.
    pub fn from_env() -> Self {
        Self::new(
            EngineSettings::from_env(|name| std::env::var(name)),
            roots_from_env(),
        )
    }

    /// From injected parts (tests).
    pub fn new(settings: EngineSettings, roots: (PathBuf, PathBuf)) -> Self {
        Self {
            settings,
            profile_root: roots.0,
            download_root: roots.1,
            sessions: Mutex::new(HashMap::new()),
        }
    }

    /// Whether a real engine can be built right now, and why not when it cannot.
    pub fn availability(&self) -> Result<PathBuf, String> {
        self.settings.availability()
    }

    fn session_dir(&self, root: &Path, session_id: &str) -> Result<PathBuf, AppError> {
        // The session id reaches a filesystem path, so it is validated before it is
        // joined: a traversal in the id would otherwise choose the directory.
        let safe = validate_session_id(session_id)?;
        Ok(root.join(safe))
    }

    /// The session's engine, or `not_found`.
    async fn session(&self, session_id: &str) -> Result<Arc<Session>, AppError> {
        let safe = validate_session_id(session_id)?;
        let sessions = self.sessions.lock().await;
        sessions.get(&safe).cloned().ok_or_else(|| {
            AppError::new(
                codes::NOT_FOUND,
                format!("Browser session not found: {safe}"),
            )
        })
    }

    /// Attach a browser to `session_id`, or answer the one already attached.
    ///
    /// Idempotent on purpose: the gateway's `controller_for` creates a controller once
    /// per session and hands back the cached one, so a second `OpenUrl` on a live
    /// session must reuse the browser rather than replace it (a replacement would
    /// lose the page the previous action navigated to).
    async fn attach(&self, session_id: &str, headless: bool) -> Result<Arc<Session>, AppError> {
        let safe = validate_session_id(session_id)?;
        let mut sessions = self.sessions.lock().await;
        if let Some(existing) = sessions.get(&safe) {
            return Ok(existing.clone());
        }
        let chromium = self
            .settings
            .availability()
            .map_err(|reason| AppError::new(codes::INTERNAL, reason))?;
        let profile_dir = self.session_dir(&self.profile_root, &safe)?;
        let download_dir = self.session_dir(&self.download_root, &safe)?;
        let _ = headless;
        let engine = Engine::launch(&chromium, &profile_dir, self.settings.no_sandbox).await?;
        let session = Arc::new(Session {
            engine: Mutex::new(engine),
            download_dir,
        });
        sessions.insert(safe, session.clone());
        Ok(session)
    }

    /// Close and forget a session, removing its profile and staged downloads.
    ///
    /// Reached over the wire by `CloseSession`. Idempotent: a session this process
    /// never had answers `Ok(false)`, because the gateway's registry is the authority
    /// on whether the session existed and a double close must not be an error.
    pub async fn detach(&self, session_id: &str) -> Result<bool, AppError> {
        let safe = validate_session_id(session_id)?;
        let session = self.sessions.lock().await.remove(&safe);
        let Some(session) = session else {
            return Ok(false);
        };
        // The map entry is gone, so no new call can reach this engine; a call already
        // holding the guard finishes first, which is what the mutex is for.
        session.engine.lock().await.close().await;
        let _ = tokio::fs::remove_dir_all(&session.download_dir).await;
        Ok(true)
    }
}

/// The profile and download roots.
///
/// Both default under the system temporary directory and both are overridable,
/// because a deployment that wants profiles on a durable volume should not have to
/// patch the binary. They are separate roots so a download sweep cannot delete a
/// live profile.
fn roots_from_env() -> (PathBuf, PathBuf) {
    let base = std::env::temp_dir().join("deepseek-browser-engine");
    let profile = std::env::var_os(PROFILE_ROOT_ENV)
        .map(PathBuf::from)
        .unwrap_or_else(|| base.join("profiles"));
    let download = std::env::var_os(DOWNLOAD_ROOT_ENV)
        .map(PathBuf::from)
        .unwrap_or_else(|| base.join("downloads"));
    (profile, download)
}

/// The oracle's `validate_session_id`: 4–80 characters of `[A-Za-z0-9_-]`.
fn validate_session_id(value: &str) -> Result<String, AppError> {
    let safe = value.trim();
    let valid = (4..=80).contains(&safe.chars().count())
        && safe
            .chars()
            .all(|character| character.is_ascii_alphanumeric() || matches!(character, '_' | '-'));
    if !valid {
        return Err(AppError::new(
            codes::INVALID_PAYLOAD,
            "Invalid browser session id",
        ));
    }
    Ok(safe.to_string())
}

/// `validate_fence` from `deepseek-protocol`, as a gRPC status.
fn admit(fence: Option<&ActionFence>) -> Result<(), Status> {
    let fence = fence.ok_or_else(|| Status::invalid_argument("ACTION_FENCE_MISSING"))?;
    deepseek_protocol::validate_fence(fence).map_err(|error| Status::invalid_argument(error.code()))
}

/// Map an engine error onto a gRPC status the gateway can translate back.
///
/// The code travels in the status metadata so the gateway does not have to parse a
/// message to recover it: the oracle's envelope needs the code, and a message-only
/// mapping would force string matching.
fn status_for(error: AppError) -> Status {
    let code = error.code;
    let message = error.message;
    let status = match code {
        codes::INVALID_PAYLOAD => Status::invalid_argument(message),
        codes::NOT_FOUND => Status::not_found(message),
        codes::UPSTREAM_TIMEOUT => Status::deadline_exceeded(message),
        _ => Status::internal(message),
    };
    let mut status = status;
    if let Ok(value) = tonic::metadata::MetadataValue::try_from(code) {
        status.metadata_mut().insert("deepseek-error-code", value);
    }
    status
}

/// The `ErrorDetail` a *successful* envelope carries when the action itself failed.
///
/// The proto's responses each have an `error` field beside their payload, which is
/// how a page-level failure (an element that is not there) differs from a transport
/// failure: the call succeeded, the action did not.
fn detail(error: &AppError) -> ErrorDetail {
    ErrorDetail {
        code: error.code.to_string(),
        category: "browser_engine".to_string(),
        message: error.message.clone(),
    }
}

#[tonic::async_trait]
impl BrowserEngineApi for BrowserEngineService {
    async fn status(
        &self,
        request: Request<StatusRequest>,
    ) -> Result<Response<StatusResponse>, Status> {
        admit(request.get_ref().fence.as_ref())?;
        match self.availability() {
            Ok(_) => Ok(Response::new(StatusResponse {
                available: true,
                engine_kind: ENGINE_KIND_CDP.to_string(),
                // Filled by `version` on first use rather than at startup: reporting a
                // revision this process has not read would be a claim, not a
                // measurement.
                chromium_revision: String::new(),
                reason: String::new(),
                error: None,
            })),
            Err(reason) => Ok(Response::new(StatusResponse {
                available: false,
                engine_kind: String::new(),
                chromium_revision: String::new(),
                reason: reason.clone(),
                error: Some(ErrorDetail {
                    code: ENGINE_NOT_CONFIGURED.to_string(),
                    category: "browser_engine".to_string(),
                    message: reason,
                }),
            })),
        }
    }

    async fn open_url(
        &self,
        request: Request<OpenUrlRequest>,
    ) -> Result<Response<PageResponse>, Status> {
        let request = request.into_inner();
        admit(request.fence.as_ref())?;
        let session = self
            .attach(&request.session_id, true)
            .await
            .map_err(status_for)?;
        let mut engine = session.engine.lock().await;
        if let Err(error) = engine.open_url(&request.url).await {
            return Ok(Response::new(PageResponse {
                page: None,
                error: Some(detail(&error)),
            }));
        }
        let page = engine.read_page("").await.map_err(status_for)?;
        Ok(Response::new(PageResponse {
            page: Some(page),
            error: None,
        }))
    }

    async fn read_page(
        &self,
        request: Request<ReadPageRequest>,
    ) -> Result<Response<PageResponse>, Status> {
        let request = request.into_inner();
        admit(request.fence.as_ref())?;
        let session = self
            .session(&request.session_id)
            .await
            .map_err(status_for)?;
        let mut engine = session.engine.lock().await;
        let page = engine
            .read_page(&request.selector)
            .await
            .map_err(status_for)?;
        Ok(Response::new(PageResponse {
            page: Some(page),
            error: None,
        }))
    }

    async fn extract_links(
        &self,
        request: Request<SessionRequest>,
    ) -> Result<Response<LinksResponse>, Status> {
        let request = request.into_inner();
        admit(request.fence.as_ref())?;
        let session = self
            .session(&request.session_id)
            .await
            .map_err(status_for)?;
        let mut engine = session.engine.lock().await;
        let links = engine.extract_links("").await.map_err(status_for)?;
        let url = engine.current_url().to_string();
        Ok(Response::new(LinksResponse {
            url,
            links: links
                .into_iter()
                .map(|link| ProtoLink {
                    href: link.href,
                    text: link.text,
                    title: link.title,
                })
                .collect(),
            error: None,
        }))
    }

    async fn screenshot(
        &self,
        request: Request<ScreenshotRequest>,
    ) -> Result<Response<ScreenshotResponse>, Status> {
        let request = request.into_inner();
        admit(request.fence.as_ref())?;
        let session = self
            .session(&request.session_id)
            .await
            .map_err(status_for)?;
        let mut engine = session.engine.lock().await;
        let url = engine.current_url().to_string();
        match engine.screenshot(&request.selector).await {
            Ok(data) => Ok(Response::new(ScreenshotResponse {
                url,
                mime_type: "image/png".to_string(),
                data,
                selector: request.selector,
                error: None,
            })),
            Err(error) => Ok(Response::new(ScreenshotResponse {
                url,
                mime_type: "image/png".to_string(),
                data: Vec::new(),
                selector: request.selector,
                error: Some(detail(&error)),
            })),
        }
    }

    async fn click(
        &self,
        request: Request<SelectorRequest>,
    ) -> Result<Response<ActionResponse>, Status> {
        let request = request.into_inner();
        admit(request.fence.as_ref())?;
        let session = self
            .session(&request.session_id)
            .await
            .map_err(status_for)?;
        let mut engine = session.engine.lock().await;
        match engine.click(&request.selector).await {
            Ok(selector) => Ok(Response::new(ActionResponse {
                url: engine.current_url().to_string(),
                selector,
                error: None,
            })),
            Err(error) => Ok(Response::new(ActionResponse {
                url: engine.current_url().to_string(),
                selector: request.selector,
                error: Some(detail(&error)),
            })),
        }
    }

    async fn type_text(
        &self,
        request: Request<TypeTextRequest>,
    ) -> Result<Response<ActionResponse>, Status> {
        let request = request.into_inner();
        admit(request.fence.as_ref())?;
        let session = self
            .session(&request.session_id)
            .await
            .map_err(status_for)?;
        let mut engine = session.engine.lock().await;
        match engine.type_text(&request.selector, &request.text).await {
            Ok(()) => Ok(Response::new(ActionResponse {
                url: engine.current_url().to_string(),
                selector: request.selector,
                error: None,
            })),
            Err(error) => Ok(Response::new(ActionResponse {
                url: engine.current_url().to_string(),
                selector: request.selector,
                error: Some(detail(&error)),
            })),
        }
    }

    async fn select(
        &self,
        request: Request<SelectRequest>,
    ) -> Result<Response<SelectResponse>, Status> {
        let request = request.into_inner();
        admit(request.fence.as_ref())?;
        let session = self
            .session(&request.session_id)
            .await
            .map_err(status_for)?;
        let mut engine = session.engine.lock().await;
        match engine.select(&request.selector, &request.value).await {
            Ok(selected) => Ok(Response::new(SelectResponse {
                url: engine.current_url().to_string(),
                selector: request.selector,
                value: request.value,
                selected,
                error: None,
            })),
            Err(error) => Ok(Response::new(SelectResponse {
                url: engine.current_url().to_string(),
                selector: request.selector,
                value: request.value,
                selected: Vec::new(),
                error: Some(detail(&error)),
            })),
        }
    }

    async fn scroll(
        &self,
        request: Request<ScrollRequest>,
    ) -> Result<Response<ActionResponse>, Status> {
        let request = request.into_inner();
        admit(request.fence.as_ref())?;
        let session = self
            .session(&request.session_id)
            .await
            .map_err(status_for)?;
        let mut engine = session.engine.lock().await;
        match engine.scroll(request.x, request.y).await {
            Ok(()) => Ok(Response::new(ActionResponse {
                url: engine.current_url().to_string(),
                selector: String::new(),
                error: None,
            })),
            Err(error) => Ok(Response::new(ActionResponse {
                url: engine.current_url().to_string(),
                selector: String::new(),
                error: Some(detail(&error)),
            })),
        }
    }

    async fn download(
        &self,
        request: Request<DownloadRequest>,
    ) -> Result<Response<DownloadResponse>, Status> {
        let request = request.into_inner();
        admit(request.fence.as_ref())?;
        let session = self
            .session(&request.session_id)
            .await
            .map_err(status_for)?;
        let mut engine = session.engine.lock().await;
        let url = engine.current_url().to_string();
        if request.selector.is_empty() {
            return Ok(Response::new(DownloadResponse {
                url,
                filename: String::new(),
                data: Vec::new(),
                error: Some(detail(&AppError::new(
                    codes::INVALID_PAYLOAD,
                    "a download selector is required; the sidecar does not fetch URLs",
                ))),
            }));
        }
        match engine
            .download(&request.selector, &session.download_dir)
            .await
        {
            Ok((filename, data)) => Ok(Response::new(DownloadResponse {
                url,
                filename,
                data,
                error: None,
            })),
            Err(error) => Ok(Response::new(DownloadResponse {
                url,
                filename: String::new(),
                data: Vec::new(),
                error: Some(detail(&error)),
            })),
        }
    }

    async fn close_session(
        &self,
        request: Request<CloseSessionRequest>,
    ) -> Result<Response<CloseSessionResponse>, Status> {
        let request = request.into_inner();
        admit(request.fence.as_ref())?;
        // A malformed session id is still an invalid argument — closing a traversal
        // must not be a no-op that reports success.
        match self.detach(&request.session_id).await {
            Ok(closed) => Ok(Response::new(CloseSessionResponse {
                closed,
                error: None,
            })),
            Err(error) => Err(status_for(error)),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fence() -> ActionFence {
        ActionFence {
            action_id: "act-1".to_string(),
            execution_epoch: 1,
        }
    }

    fn service(settings: EngineSettings) -> BrowserEngineService {
        let root = std::env::temp_dir().join("deepseek-browser-engine-test");
        BrowserEngineService::new(settings, (root.join("profiles"), root.join("downloads")))
    }

    #[test]
    fn session_ids_are_validated_before_they_reach_a_path() {
        assert_eq!(
            validate_session_id("browser_0123456789abcdef").unwrap(),
            "browser_0123456789abcdef"
        );
        assert!(validate_session_id("../escape").is_err());
        assert!(validate_session_id("ab").is_err());
        assert!(validate_session_id("has space").is_err());
        assert!(validate_session_id(&"a".repeat(81)).is_err());
        assert!(validate_session_id("a/b").is_err());
    }

    #[test]
    fn a_missing_fence_is_refused() {
        assert_eq!(
            admit(None).unwrap_err().code(),
            tonic::Code::InvalidArgument
        );
        assert_eq!(admit(None).unwrap_err().message(), "ACTION_FENCE_MISSING");
    }

    #[test]
    fn an_empty_or_zero_fence_is_refused_before_any_work() {
        let empty = ActionFence {
            action_id: String::new(),
            execution_epoch: 1,
        };
        assert_eq!(
            admit(Some(&empty)).unwrap_err().message(),
            "EMPTY_ACTION_ID"
        );
        let zero = ActionFence {
            action_id: "act-1".to_string(),
            execution_epoch: 0,
        };
        assert_eq!(
            admit(Some(&zero)).unwrap_err().message(),
            "ZERO_EXECUTION_EPOCH"
        );
        assert!(admit(Some(&fence())).is_ok());
    }

    #[tokio::test]
    async fn a_status_without_an_engine_reports_why_rather_than_claiming_one() {
        let service = service(EngineSettings::default());
        let response = service
            .status(Request::new(StatusRequest {
                fence: Some(fence()),
            }))
            .await
            .expect("status is a read")
            .into_inner();
        assert!(!response.available);
        assert_eq!(response.engine_kind, "");
        let error = response.error.expect("the refusal carries a code");
        assert_eq!(error.code, ENGINE_NOT_CONFIGURED);
    }

    #[tokio::test]
    async fn a_status_with_a_configured_browser_reports_the_cdp_kind() {
        // The file only has to exist: `Status` reports configuration, and the first
        // real action is what proves the binary can be driven.
        let browser = std::env::temp_dir().join("deepseek-browser-engine-fake-chromium");
        std::fs::write(&browser, b"not really a browser").expect("write placeholder");
        let service = service(EngineSettings {
            chromium: Some(browser.clone()),
            no_sandbox: true,
        });
        let response = service
            .status(Request::new(StatusRequest {
                fence: Some(fence()),
            }))
            .await
            .expect("status is a read")
            .into_inner();
        assert!(response.available);
        assert_eq!(response.engine_kind, ENGINE_KIND_CDP);
        assert!(response.error.is_none());
        let _ = std::fs::remove_file(browser);
    }

    #[tokio::test]
    async fn an_action_on_an_unknown_session_is_not_found_rather_than_creating_one() {
        let browser = std::env::temp_dir().join("deepseek-browser-engine-fake-chromium-2");
        std::fs::write(&browser, b"not really a browser").expect("write placeholder");
        let service = service(EngineSettings {
            chromium: Some(browser.clone()),
            no_sandbox: true,
        });
        let error = service
            .read_page(Request::new(ReadPageRequest {
                fence: Some(fence()),
                session_id: "browser_deadbeefdeadbeef".to_string(),
                selector: String::new(),
            }))
            .await
            .expect_err("an unknown session must not create a browser");
        assert_eq!(error.code(), tonic::Code::NotFound);
        let _ = std::fs::remove_file(browser);
    }

    #[tokio::test]
    async fn an_invalid_session_id_is_an_invalid_argument() {
        let service = service(EngineSettings::default());
        let error = service
            .read_page(Request::new(ReadPageRequest {
                fence: Some(fence()),
                session_id: "../escape".to_string(),
                selector: String::new(),
            }))
            .await
            .expect_err("a traversal in the session id must be refused");
        assert_eq!(error.code(), tonic::Code::InvalidArgument);
    }

    #[tokio::test]
    async fn detaching_an_unknown_session_is_a_no_op_not_an_error() {
        let service = service(EngineSettings::default());
        assert!(!service.detach("browser_deadbeefdeadbeef").await.unwrap());
    }
}
