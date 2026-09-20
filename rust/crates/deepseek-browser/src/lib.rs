//! The browser engine sidecar (ADR-0050).
//!
//! Stage 1 is the **boundary**, not the engine. This process owns a browser in the
//! finished design; today it owns nothing, and it says so: `Status` reports
//! `available: false` and every action refuses with `unimplemented`.
//!
//! That refusal is the whole point of shipping the skeleton first. The gateway's
//! switch is `playwright_available()`, which stays `false` until an engine answers,
//! so a boundary that refuses keeps today's behaviour exactly: the static controller
//! runs, and its parity is already pinned by `browser_page_parity_probe`. A skeleton
//! that pretended to work would be worse than one that admits it does not.
//!
//! Every request binds an `ActionFence`; the engine takes it as the call's ordering
//! id, as ADR-0049 requires of every native boundary.

use deepseek_protocol::generated::deepseek::browser::v1::{
    ActionResponse, DownloadRequest, DownloadResponse, LinksResponse, OpenUrlRequest, PageResponse,
    ReadPageRequest, ScreenshotRequest, ScreenshotResponse, ScrollRequest, SelectRequest,
    SelectResponse, SelectorRequest, SessionRequest, StatusRequest, StatusResponse,
    TypeTextRequest, browser_engine_server::BrowserEngine as BrowserEngineApi,
};
use deepseek_protocol::generated::deepseek::common::v1::ErrorDetail;
use tonic::{Request, Response, Status};

/// The kind a real engine will report once CDP drives Chromium.
pub const ENGINE_KIND_CDP: &str = "cdp_chromium";
/// The machine code carried in `StatusResponse.error` while there is no engine.
pub const ENGINE_NOT_IMPLEMENTED: &str = "browser_engine_not_implemented";
/// One line, used as both the refusal message and the `Status` reason.
pub const NOT_IMPLEMENTED_REASON: &str = "browser engine is not implemented yet (ADR-0050 stage 1)";

/// The engine stub. It answers `Status` honestly and refuses everything else.
#[derive(Debug, Default)]
pub struct BrowserEngine;

impl BrowserEngine {
    fn refuse<T>(&self) -> Result<Response<T>, Status> {
        Err(Status::unimplemented(NOT_IMPLEMENTED_REASON))
    }
}

#[tonic::async_trait]
impl BrowserEngineApi for BrowserEngine {
    async fn status(
        &self,
        _request: Request<StatusRequest>,
    ) -> Result<Response<StatusResponse>, Status> {
        Ok(Response::new(StatusResponse {
            available: false,
            engine_kind: String::new(),
            chromium_revision: String::new(),
            reason: NOT_IMPLEMENTED_REASON.to_string(),
            error: Some(ErrorDetail {
                code: ENGINE_NOT_IMPLEMENTED.to_string(),
                category: "browser_engine".to_string(),
                message: NOT_IMPLEMENTED_REASON.to_string(),
            }),
        }))
    }

    async fn open_url(
        &self,
        _request: Request<OpenUrlRequest>,
    ) -> Result<Response<PageResponse>, Status> {
        self.refuse()
    }

    async fn read_page(
        &self,
        _request: Request<ReadPageRequest>,
    ) -> Result<Response<PageResponse>, Status> {
        self.refuse()
    }

    async fn extract_links(
        &self,
        _request: Request<SessionRequest>,
    ) -> Result<Response<LinksResponse>, Status> {
        self.refuse()
    }

    async fn screenshot(
        &self,
        _request: Request<ScreenshotRequest>,
    ) -> Result<Response<ScreenshotResponse>, Status> {
        self.refuse()
    }

    async fn click(
        &self,
        _request: Request<SelectorRequest>,
    ) -> Result<Response<ActionResponse>, Status> {
        self.refuse()
    }

    async fn type_text(
        &self,
        _request: Request<TypeTextRequest>,
    ) -> Result<Response<ActionResponse>, Status> {
        self.refuse()
    }

    async fn select(
        &self,
        _request: Request<SelectRequest>,
    ) -> Result<Response<SelectResponse>, Status> {
        self.refuse()
    }

    async fn scroll(
        &self,
        _request: Request<ScrollRequest>,
    ) -> Result<Response<ActionResponse>, Status> {
        self.refuse()
    }

    async fn download(
        &self,
        _request: Request<DownloadRequest>,
    ) -> Result<Response<DownloadResponse>, Status> {
        self.refuse()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fence() -> deepseek_protocol::generated::deepseek::common::v1::ActionFence {
        deepseek_protocol::generated::deepseek::common::v1::ActionFence {
            action_id: "test-action".to_string(),
            execution_epoch: 1,
        }
    }

    #[tokio::test]
    async fn status_reports_no_engine_rather_than_claiming_one() {
        let response = BrowserEngine
            .status(Request::new(StatusRequest {
                fence: Some(fence()),
            }))
            .await
            .expect("status is a read, not a refusal")
            .into_inner();
        assert!(!response.available);
        assert_eq!(response.engine_kind, "");
        assert_eq!(response.reason, NOT_IMPLEMENTED_REASON);
        let error = response.error.expect("the refusal carries a code");
        assert_eq!(error.code, ENGINE_NOT_IMPLEMENTED);
    }

    #[tokio::test]
    async fn every_action_refuses_while_there_is_no_engine() {
        let engine = BrowserEngine;
        let refusals = [
            engine
                .open_url(Request::new(OpenUrlRequest {
                    fence: Some(fence()),
                    session_id: String::new(),
                    url: "https://example.com/".to_string(),
                }))
                .await
                .is_err(),
            engine
                .read_page(Request::new(ReadPageRequest {
                    fence: Some(fence()),
                    session_id: String::new(),
                    selector: String::new(),
                }))
                .await
                .is_err(),
            engine
                .extract_links(Request::new(SessionRequest {
                    fence: Some(fence()),
                    session_id: String::new(),
                }))
                .await
                .is_err(),
            engine
                .screenshot(Request::new(ScreenshotRequest {
                    fence: Some(fence()),
                    session_id: String::new(),
                    selector: String::new(),
                }))
                .await
                .is_err(),
            engine
                .click(Request::new(SelectorRequest {
                    fence: Some(fence()),
                    session_id: String::new(),
                    selector: String::new(),
                }))
                .await
                .is_err(),
            engine
                .type_text(Request::new(TypeTextRequest {
                    fence: Some(fence()),
                    session_id: String::new(),
                    selector: String::new(),
                    text: String::new(),
                }))
                .await
                .is_err(),
            engine
                .select(Request::new(SelectRequest {
                    fence: Some(fence()),
                    session_id: String::new(),
                    selector: String::new(),
                    value: String::new(),
                }))
                .await
                .is_err(),
            engine
                .scroll(Request::new(ScrollRequest {
                    fence: Some(fence()),
                    session_id: String::new(),
                    x: 0,
                    y: 600,
                }))
                .await
                .is_err(),
            engine
                .download(Request::new(DownloadRequest {
                    fence: Some(fence()),
                    session_id: String::new(),
                    url: String::new(),
                    selector: String::new(),
                }))
                .await
                .is_err(),
        ];
        assert!(
            refusals.iter().all(|refused| *refused),
            "an action answered"
        );
        let status = engine
            .open_url(Request::new(OpenUrlRequest {
                fence: Some(fence()),
                session_id: String::new(),
                url: "https://example.com/".to_string(),
            }))
            .await
            .expect_err("open_url must refuse");
        assert_eq!(status.code(), tonic::Code::Unimplemented);
        assert_eq!(status.message(), NOT_IMPLEMENTED_REASON);
    }
}
