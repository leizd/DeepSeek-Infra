//! The browser engine client: the gateway's implementation of the policy crate's
//! [`deepseek_policy::browser_engine::BrowserEngine`] seam.
//!
//! # Why there is a worker thread
//!
//! Two shapes meet here and neither can move:
//!
//! - The policy crate is **synchronous**. Its `browser_*` branch runs inside
//!   `spawn_blocking`, alongside the data branches that take OS file locks, so the
//!   seam it declares is a blocking call.
//! - The engine is reached over **tonic**, which is asynchronous, and the generated
//!   client owns a `Channel` that wants a runtime.
//!
//! So this type owns one dedicated OS thread with its own single-threaded runtime,
//! holds the generated gRPC client there, and answers blocking calls over a channel.
//! That is the same bridge `fetch_provider` uses for the opposite reason (a blocking
//! HTTP client on an async runtime); here the blocking side is the caller.
//!
//! # What is deliberately not here
//!
//! No retry, no reconnect loop, no connection pool: the engine is a loopback sidecar
//! and a call that fails is a call that failed. `Status` is probed once at
//! construction, and a deployment whose engine is absent gets the static controller
//! rather than a retry storm.
//!
//! # Fences
//!
//! Every request carries the `ActionFence` the policy crate built from the tool
//! call. This client never invents an epoch and never advances one: a worker must not
//! promote itself because a caller sent a larger number.

use std::sync::mpsc::Sender;
use std::sync::{Arc, Mutex};

use deepseek_policy::browser_engine::{
    BrowserEngine, EngineAction, EngineDownload, EngineError, EngineFence, EngineLink,
    EngineOutcome, EngineRequest, EngineStatus,
};
use deepseek_protocol::generated::deepseek::browser::v1::{
    CloseSessionRequest, DownloadRequest, OpenUrlRequest, ReadPageRequest, ScreenshotRequest,
    ScrollRequest, SelectRequest, SelectorRequest, SessionRequest, StatusRequest, TypeTextRequest,
    browser_engine_client::BrowserEngineClient,
};
use deepseek_protocol::generated::deepseek::common::v1::ActionFence;
use tokio::sync::oneshot;

/// The sidecar's default loopback address, matching `deepseek-browser`'s `main`.
pub const DEFAULT_ENGINE_ADDR: &str = "http://127.0.0.1:50053";
/// Where a deployment points the gateway at its engine.
pub const ENGINE_ADDR_ENV: &str = "DEEPSEEK_BROWSER_ENGINE_ADDR";

/// The answer a worker job produces.
type Reply = oneshot::Sender<Result<EngineOutcome, EngineError>>;
/// One job for the worker thread: it runs on the worker's own runtime and returns
/// the engine's answer.
type Job = Box<
    dyn FnOnce(
            &mut BrowserEngineClient<tonic::transport::Channel>,
            &mut tokio::runtime::Runtime,
        ) -> Result<EngineOutcome, EngineError>
        + Send,
>;

/// The engine, reached over gRPC from a dedicated runtime thread.
pub struct GrpcBrowserEngine {
    jobs: Mutex<Option<Sender<Job>>>,
    status: EngineStatus,
}

impl GrpcBrowserEngine {
    /// Connect to `addr` and probe `Status`.
    ///
    /// Returns `None` when no engine answers — the deployment that has no browser,
    /// which the policy crate reports as `playwright_available() == false` and
    /// serves with the static controller.
    ///
    /// Refuses to run inside an async context: this method blocks on its own runtime,
    /// and `Runtime::block_on` panics when called from a thread that already has one.
    /// The browser branch runs under `spawn_blocking`, which has no runtime handle, so
    /// the legitimate call sites are unaffected — a caller that is inside a runtime is
    /// asking for the engine from a place where it cannot be built.
    pub fn connect(addr: &str) -> Option<Arc<Self>> {
        if tokio::runtime::Handle::try_current().is_ok() {
            eprintln!(
                "deepseek-gateway: refusing to connect to the browser engine from inside \
                 an async runtime; construct it at startup instead"
            );
            return None;
        }
        let endpoint = tonic::transport::Endpoint::from_shared(addr.to_string()).ok()?;
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .ok()?;
        let channel = runtime.block_on(endpoint.connect()).ok()?;
        let mut client = BrowserEngineClient::new(channel);

        // Probe once, on this thread, before the worker starts: a status the gateway
        // reports must be a status it measured.
        let probe = runtime.block_on(client.status(StatusRequest {
            fence: Some(proto_fence(&EngineFence::for_request("engine-status", 1))),
        }));
        let status = match probe {
            Ok(response) => {
                let response = response.into_inner();
                EngineStatus {
                    available: response.available,
                    engine_kind: response.engine_kind,
                    chromium_revision: response.chromium_revision,
                    reason: response.reason,
                }
            }
            Err(status) => EngineStatus {
                available: false,
                engine_kind: String::new(),
                chromium_revision: String::new(),
                reason: format!("the browser engine refused Status: {}", status.message()),
            },
        };

        let (sender, receiver) = std::sync::mpsc::channel::<Job>();
        let handle = std::thread::Builder::new()
            .name("deepseek-browser-engine".to_string())
            .spawn(move || {
                let mut runtime = runtime;
                let mut client = client;
                // The receiver ends when the sender is dropped, which is when this
                // process stops holding the engine.
                while let Ok(job) = receiver.recv() {
                    // The answer already went back through the job's reply channel;
                    // this result is only here so a panic in one job cannot silently
                    // become a success for the caller.
                    let _ = job(&mut client, &mut runtime);
                }
            })
            .ok()?;
        // The thread is detached on purpose: it owns no state a caller can observe
        // except through the channel, and it exits when that channel closes.
        drop(handle);

        Some(Arc::new(Self {
            jobs: Mutex::new(Some(sender)),
            status,
        }))
    }

    /// Connect from the server environment, defaulting to the sidecar's loopback
    /// address when the variable is unset.
    pub fn from_env() -> Option<Arc<Self>> {
        let addr = std::env::var(ENGINE_ADDR_ENV)
            .ok()
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| DEFAULT_ENGINE_ADDR.to_string());
        Self::connect(&addr)
    }

    /// Run one job on the worker thread and block for its answer.
    fn dispatch<F>(&self, build: F) -> Result<EngineOutcome, EngineError>
    where
        F: FnOnce() -> Job,
    {
        let (reply, receiver) = oneshot::channel::<Result<EngineOutcome, EngineError>>();
        {
            let guard = self
                .jobs
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            let Some(jobs) = guard.as_ref() else {
                return Err(EngineError::new(
                    deepseek_policy::browser_engine::ENGINE_NOT_CONFIGURED,
                    "the browser engine worker is not running",
                ));
            };
            let job = build();
            // If the worker has stopped, `send` fails and the job is dropped, which
            // drops the reply sender and settles the receiver as an error.
            if jobs.send(wrap_job(job, reply)).is_err() {
                return Err(EngineError::new(
                    deepseek_policy::browser_engine::ENGINE_NOT_CONFIGURED,
                    "the browser engine worker stopped",
                ));
            }
        }
        receiver.blocking_recv().unwrap_or_else(|_| {
            Err(EngineError::new(
                deepseek_policy::browser_engine::ENGINE_NOT_CONFIGURED,
                "the browser engine worker dropped the call",
            ))
        })
    }
}

impl Drop for GrpcBrowserEngine {
    fn drop(&mut self) {
        // Closing the channel is what stops the worker thread.
        let _ = self
            .jobs
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take();
    }
}

fn proto_fence(fence: &EngineFence) -> ActionFence {
    ActionFence {
        action_id: fence.action_id.clone(),
        execution_epoch: fence.execution_epoch,
    }
}

/// Wrap a job so its answer is sent back to the caller.
///
/// A named function rather than an inline closure: the coercion from a concrete
/// closure to the boxed `Job` trait object needs the parameter's type, which an
/// inline `Box::new(|..| ..)` does not provide.
fn wrap_job(job: Job, reply: Reply) -> Job {
    Box::new(move |client, runtime| {
        let outcome = job(client, runtime);
        match outcome {
            Ok(value) => {
                let _ = reply.send(Ok(value.clone()));
                Ok(value)
            }
            Err(error) => {
                let _ = reply.send(Err(error.clone()));
                Err(error)
            }
        }
    })
}

/// Map a tonic status onto the policy crate's engine error, keeping the code the
/// sidecar put in the metadata.
fn engine_error(status: tonic::Status) -> EngineError {
    let code = status
        .metadata()
        .get("deepseek-error-code")
        .and_then(|value| value.to_str().ok())
        .unwrap_or_else(|| match status.code() {
            tonic::Code::InvalidArgument => deepseek_policy::app_error::codes::INVALID_PAYLOAD,
            tonic::Code::NotFound => deepseek_policy::browser_engine::ENGINE_SESSION_NOT_FOUND,
            tonic::Code::DeadlineExceeded => deepseek_policy::app_error::codes::UPSTREAM_TIMEOUT,
            _ => deepseek_policy::app_error::codes::INTERNAL,
        })
        .to_string();
    EngineError::new(code, status.message().to_string())
}

/// The `ErrorDetail` a successful envelope carries when the *action* failed, which
/// is how the sidecar reports "the element is not there" without failing the call.
fn action_error(
    error: Option<deepseek_protocol::generated::deepseek::common::v1::ErrorDetail>,
) -> Result<(), EngineError> {
    match error {
        None => Ok(()),
        Some(detail) => Err(EngineError::new(detail.code, detail.message)),
    }
}

fn outcome_from_page(
    page: deepseek_protocol::generated::deepseek::browser::v1::Page,
    engine_kind: &str,
) -> EngineOutcome {
    EngineOutcome {
        engine_kind: engine_kind.to_string(),
        url: page.url,
        title: page.title,
        text: page.text,
        html: page.html,
        selector: page.selector,
        ..Default::default()
    }
}

fn links_from(
    links: Vec<deepseek_protocol::generated::deepseek::browser::v1::Link>,
) -> Vec<EngineLink> {
    links
        .into_iter()
        .map(|link| EngineLink {
            href: link.href,
            text: link.text,
            title: link.title,
        })
        .collect()
}

impl BrowserEngine for GrpcBrowserEngine {
    fn status(&self, _fence: &EngineFence) -> Result<EngineStatus, EngineError> {
        // Measured once at construction, on the worker's own runtime. Re-probing per
        // call would make every `browser_*` action pay a round trip to learn what the
        // gateway already knows.
        Ok(self.status.clone())
    }

    fn execute(
        &self,
        fence: &EngineFence,
        request: &EngineRequest,
    ) -> Result<EngineOutcome, EngineError> {
        let engine_fence = fence.clone();
        let request = request.clone();
        self.dispatch(move || {
            Box::new(move |client, runtime| {
                runtime.block_on(async move {
                    let proto_fence = Some(proto_fence(&engine_fence));
                    let session_id = request.session_id.clone();
                    match request.action {
                        EngineAction::Status => {
                            let response = client
                                .status(StatusRequest { fence: proto_fence })
                                .await
                                .map_err(engine_error)?
                                .into_inner();
                            action_error(response.error)?;
                            Ok(EngineOutcome {
                                engine_kind: response.engine_kind,
                                ..Default::default()
                            })
                        }
                        EngineAction::OpenUrl => {
                            let response = client
                                .open_url(OpenUrlRequest {
                                    fence: proto_fence,
                                    session_id,
                                    url: request.url.clone(),
                                })
                                .await
                                .map_err(engine_error)?
                                .into_inner();
                            action_error(response.error)?;
                            let page = response.page.ok_or_else(|| {
                                EngineError::new(
                                    deepseek_policy::app_error::codes::INTERNAL,
                                    "the engine answered OpenUrl with no page",
                                )
                            })?;
                            Ok(outcome_from_page(
                                page,
                                deepseek_policy::browser_engine::ENGINE_KIND_CDP,
                            ))
                        }
                        EngineAction::ReadPage => {
                            let response = client
                                .read_page(ReadPageRequest {
                                    fence: proto_fence,
                                    session_id,
                                    selector: request.selector.clone(),
                                })
                                .await
                                .map_err(engine_error)?
                                .into_inner();
                            action_error(response.error)?;
                            let page = response.page.ok_or_else(|| {
                                EngineError::new(
                                    deepseek_policy::app_error::codes::INTERNAL,
                                    "the engine answered ReadPage with no page",
                                )
                            })?;
                            Ok(outcome_from_page(
                                page,
                                deepseek_policy::browser_engine::ENGINE_KIND_CDP,
                            ))
                        }
                        EngineAction::ExtractLinks => {
                            let response = client
                                .extract_links(SessionRequest {
                                    fence: proto_fence,
                                    session_id,
                                })
                                .await
                                .map_err(engine_error)?
                                .into_inner();
                            action_error(response.error)?;
                            Ok(EngineOutcome {
                                engine_kind: deepseek_policy::browser_engine::ENGINE_KIND_CDP
                                    .to_string(),
                                url: response.url,
                                links: links_from(response.links),
                                ..Default::default()
                            })
                        }
                        EngineAction::Screenshot => {
                            let response = client
                                .screenshot(ScreenshotRequest {
                                    fence: proto_fence,
                                    session_id,
                                    selector: request.selector.clone(),
                                })
                                .await
                                .map_err(engine_error)?
                                .into_inner();
                            action_error(response.error)?;
                            Ok(EngineOutcome {
                                engine_kind: deepseek_policy::browser_engine::ENGINE_KIND_CDP
                                    .to_string(),
                                url: response.url,
                                mime_type: response.mime_type,
                                selector: response.selector,
                                screenshot: Some(response.data),
                                ..Default::default()
                            })
                        }
                        EngineAction::Click => {
                            let response = client
                                .click(SelectorRequest {
                                    fence: proto_fence,
                                    session_id,
                                    selector: request.selector.clone(),
                                })
                                .await
                                .map_err(engine_error)?
                                .into_inner();
                            action_error(response.error)?;
                            Ok(EngineOutcome {
                                engine_kind: deepseek_policy::browser_engine::ENGINE_KIND_CDP
                                    .to_string(),
                                url: response.url,
                                selector: response.selector,
                                ..Default::default()
                            })
                        }
                        EngineAction::TypeText => {
                            let response = client
                                .type_text(TypeTextRequest {
                                    fence: proto_fence,
                                    session_id,
                                    selector: request.selector.clone(),
                                    text: request.text.clone(),
                                })
                                .await
                                .map_err(engine_error)?
                                .into_inner();
                            action_error(response.error)?;
                            Ok(EngineOutcome {
                                engine_kind: deepseek_policy::browser_engine::ENGINE_KIND_CDP
                                    .to_string(),
                                url: response.url,
                                selector: response.selector,
                                ..Default::default()
                            })
                        }
                        EngineAction::Select => {
                            let response = client
                                .select(SelectRequest {
                                    fence: proto_fence,
                                    session_id,
                                    selector: request.selector.clone(),
                                    value: request.value.clone(),
                                })
                                .await
                                .map_err(engine_error)?
                                .into_inner();
                            action_error(response.error)?;
                            Ok(EngineOutcome {
                                engine_kind: deepseek_policy::browser_engine::ENGINE_KIND_CDP
                                    .to_string(),
                                url: response.url,
                                selector: response.selector,
                                selected: response.selected,
                                ..Default::default()
                            })
                        }
                        EngineAction::Scroll => {
                            let response = client
                                .scroll(ScrollRequest {
                                    fence: proto_fence,
                                    session_id,
                                    x: request.x,
                                    y: request.y,
                                })
                                .await
                                .map_err(engine_error)?
                                .into_inner();
                            action_error(response.error)?;
                            Ok(EngineOutcome {
                                engine_kind: deepseek_policy::browser_engine::ENGINE_KIND_CDP
                                    .to_string(),
                                url: response.url,
                                ..Default::default()
                            })
                        }
                        EngineAction::Download => {
                            let response = client
                                .download(DownloadRequest {
                                    fence: proto_fence,
                                    session_id,
                                    url: request.url.clone(),
                                    selector: request.selector.clone(),
                                })
                                .await
                                .map_err(engine_error)?
                                .into_inner();
                            action_error(response.error)?;
                            Ok(EngineOutcome {
                                engine_kind: deepseek_policy::browser_engine::ENGINE_KIND_CDP
                                    .to_string(),
                                url: response.url,
                                download: Some(EngineDownload {
                                    filename: response.filename,
                                    data: response.data,
                                }),
                                ..Default::default()
                            })
                        }
                        EngineAction::CloseSession => {
                            let response = client
                                .close_session(CloseSessionRequest {
                                    fence: proto_fence,
                                    session_id,
                                })
                                .await
                                .map_err(engine_error)?
                                .into_inner();
                            action_error(response.error)?;
                            Ok(EngineOutcome {
                                engine_kind: deepseek_policy::browser_engine::ENGINE_KIND_CDP
                                    .to_string(),
                                ..Default::default()
                            })
                        }
                    }
                })
            })
        })
    }
}

/// The engine for this deployment, or `None` when none answers.
///
/// Built **once per process** and cached. `ToolRoundExecutor::from_env` runs per
/// request, so an engine constructed there would open a channel and spawn a runtime
/// thread for every chat request; caching it also makes the "no engine" line print
/// once rather than on every turn.
///
/// A failed connection is **not** fatal: a deployment with no browser is the
/// static-controller deployment, which is the behaviour this repository has shipped
/// since `browser_*` was ported.
pub fn browser_engine_from_env() -> Option<Arc<dyn BrowserEngine>> {
    static ENGINE: std::sync::OnceLock<Option<Arc<dyn BrowserEngine>>> = std::sync::OnceLock::new();
    ENGINE
        .get_or_init(|| {
            let addr = std::env::var(ENGINE_ADDR_ENV)
                .ok()
                .map(|value| value.trim().to_string())
                .filter(|value| !value.is_empty())
                .unwrap_or_else(|| DEFAULT_ENGINE_ADDR.to_string());
            match GrpcBrowserEngine::connect(&addr) {
                Some(engine) => Some(engine as Arc<dyn BrowserEngine>),
                None => {
                    eprintln!(
                        "deepseek-gateway: no browser engine at {addr}; \
                         browser_* uses the static controller"
                    );
                    None
                }
            }
        })
        .clone()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_default_address_is_the_sidecars_loopback_address() {
        assert_eq!(DEFAULT_ENGINE_ADDR, "http://127.0.0.1:50053");
    }

    #[test]
    fn a_status_error_keeps_the_sidecars_code_from_the_metadata() {
        let mut status = tonic::Status::not_found("Browser session not found: x");
        status.metadata_mut().insert(
            "deepseek-error-code",
            tonic::metadata::MetadataValue::from_static("browser_session_not_found"),
        );
        let error = engine_error(status);
        assert_eq!(
            error.code,
            deepseek_policy::browser_engine::ENGINE_SESSION_NOT_FOUND
        );
        assert_eq!(error.status, 404);
    }

    #[test]
    fn a_status_error_without_metadata_falls_back_to_the_grpc_code() {
        assert_eq!(
            engine_error(tonic::Status::invalid_argument("bad")).code,
            deepseek_policy::app_error::codes::INVALID_PAYLOAD
        );
        assert_eq!(
            engine_error(tonic::Status::deadline_exceeded("slow")).code,
            deepseek_policy::app_error::codes::UPSTREAM_TIMEOUT
        );
        assert_eq!(
            engine_error(tonic::Status::internal("boom")).code,
            deepseek_policy::app_error::codes::INTERNAL
        );
    }

    #[test]
    fn an_action_level_error_is_an_error_and_an_absent_one_is_not() {
        assert!(action_error(None).is_ok());
        let detail = deepseek_protocol::generated::deepseek::common::v1::ErrorDetail {
            code: "not_found".to_string(),
            category: "browser_engine".to_string(),
            message: "element_not_found: #x".to_string(),
        };
        let error = action_error(Some(detail)).expect_err("an action error must surface");
        assert_eq!(error.code, "not_found");
        assert_eq!(error.status, 404);
    }

    #[test]
    fn a_missing_engine_is_none_not_a_panic() {
        // Port 1 is reserved and nothing listens on it.
        assert!(GrpcBrowserEngine::connect("http://127.0.0.1:1").is_none());
    }
}
